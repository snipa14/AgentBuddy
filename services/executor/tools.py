"""The Executor's typed tool functions, per ARCHITECTURE.md §4.3.

Every function here is the only sanctioned way an agent tool call can touch
a project's files or run a command -- always inside a freshly created,
locked-down workspace container (services.executor.containers), never via
direct filesystem access from the Executor's own process.
"""

import io
import json
import os
import re
import tarfile
from pathlib import Path

import docker
from docker.errors import NotFound

from services.executor.config import ExecutorConfig
from services.executor.containers import (
    WORKSPACE_MOUNT_TARGET,
    WORKSPACE_UID,
    exec_in_container,
    workspace_container,
)
from services.executor.errors import ContainerExecutionError
from services.executor.paths import resolve_project_path
from services.executor.schemas import CommandResult, FileEntry, TestResult

_PYTEST_SUMMARY_RE = re.compile(r"(?P<count>\d+)\s+(?P<outcome>passed|failed|error|errors)")
_COVERAGE_TOTAL_RE = re.compile(r"^TOTAL\s+.*\s(\d+)%\s*$", re.MULTILINE)

_LIST_DIR_SNIPPET = (
    "import json, os, sys\n"
    "path = sys.argv[1]\n"
    "entries = sorted(os.listdir(path))\n"
    "print(json.dumps([\n"
    "    {'name': e, 'is_dir': os.path.isdir(os.path.join(path, e))} for e in entries\n"
    "]))\n"
)


def tool_run_command(
    client: docker.DockerClient,
    config: ExecutorConfig,
    project_id: str,
    command: list[str],
    timeout: int | None = None,
) -> CommandResult:
    """Runs an arbitrary command inside a fresh workspace container for project_id.

    Args:
        client: A Docker SDK client.
        config: The Executor's runtime configuration.
        project_id: The project whose directory gets bind-mounted.
        command: The command to run, as an argv list (never a shell string).
        timeout: Seconds to allow before killing the container. Defaults to
            config.command_timeout_seconds.

    Returns:
        A CommandResult with stdout, stderr, and exit_code.
    """
    effective_timeout = timeout if timeout is not None else config.command_timeout_seconds
    project_host_path = _project_host_path(config, project_id)
    with workspace_container(client, config, project_host_path) as container:
        return exec_in_container(container, command, effective_timeout)


def tool_read_file(
    client: docker.DockerClient,
    config: ExecutorConfig,
    project_id: str,
    path: str,
) -> str:
    """Reads a UTF-8 text file from within project_id's directory.

    Args:
        client: A Docker SDK client.
        config: The Executor's runtime configuration.
        project_id: The project whose directory gets bind-mounted.
        path: A path relative to the project's root.

    Returns:
        The file's decoded content.

    Raises:
        PathTraversalError: If path resolves outside the project root.
        FileNotFoundError: If path doesn't exist in the project.
    """
    container_path = _container_path(path)
    project_host_path = _project_host_path(config, project_id)
    with workspace_container(client, config, project_host_path) as container:
        try:
            chunks, _ = container.get_archive(container_path)
        except NotFound as exc:
            raise FileNotFoundError(f"'{path}' not found in project '{project_id}'") from exc
        return _extract_single_file(chunks, os.path.basename(container_path))


def tool_write_file(
    client: docker.DockerClient,
    config: ExecutorConfig,
    project_id: str,
    path: str,
    content: str,
) -> bool:
    """Writes a UTF-8 text file into project_id's directory, creating parents as needed.

    Args:
        client: A Docker SDK client.
        config: The Executor's runtime configuration.
        project_id: The project whose directory gets bind-mounted.
        path: A path relative to the project's root.
        content: The text to write.

    Returns:
        True once the write has landed in the bind-mounted project directory.

    Raises:
        PathTraversalError: If path resolves outside the project root.
    """
    container_path = _container_path(path)
    directory = os.path.dirname(container_path)
    project_host_path = _project_host_path(config, project_id)
    with workspace_container(client, config, project_host_path) as container:
        container.exec_run(["mkdir", "-p", directory])
        container.put_archive(
            directory, _build_single_file_tar(os.path.basename(container_path), content)
        )
    return True


def tool_list_files(
    client: docker.DockerClient,
    config: ExecutorConfig,
    project_id: str,
    path: str = ".",
) -> list[FileEntry]:
    """Lists the immediate entries of a directory within project_id's directory.

    Args:
        client: A Docker SDK client.
        config: The Executor's runtime configuration.
        project_id: The project whose directory gets bind-mounted.
        path: A directory path relative to the project's root.

    Returns:
        A FileEntry per immediate child of path.

    Raises:
        PathTraversalError: If path resolves outside the project root.
        ContainerExecutionError: If listing the directory fails.
    """
    container_path = _container_path(path)
    project_host_path = _project_host_path(config, project_id)
    with workspace_container(client, config, project_host_path) as container:
        result = exec_in_container(
            container,
            ["python3", "-c", _LIST_DIR_SNIPPET, container_path],
            config.command_timeout_seconds,
        )
    if result.exit_code != 0:
        raise ContainerExecutionError(f"list_files failed for '{path}': {result.stderr}")
    return [FileEntry(**entry) for entry in json.loads(result.stdout)]


def tool_run_tests(
    client: docker.DockerClient,
    config: ExecutorConfig,
    project_id: str,
    framework: str = "pytest",
) -> TestResult:
    """Runs a project's test suite with coverage inside a fresh workspace container.

    Args:
        client: A Docker SDK client.
        config: The Executor's runtime configuration.
        project_id: The project whose directory gets bind-mounted.
        framework: Only "pytest" is supported for v1.

    Returns:
        A TestResult with passed/failed counts, coverage percentage (if
        parseable from the report), and the raw combined output.

    Raises:
        ValueError: If framework isn't "pytest".
    """
    if framework != "pytest":
        raise ValueError(f"unsupported test framework: {framework!r}")

    project_host_path = _project_host_path(config, project_id)
    command = ["python3", "-m", "pytest", "--cov=.", "--cov-report=term-missing", "-q"]
    with workspace_container(client, config, project_host_path) as container:
        result = exec_in_container(container, command, config.command_timeout_seconds)

    combined_output = f"{result.stdout}\n{result.stderr}"
    passed, failed = _parse_pytest_summary(combined_output)
    return TestResult(
        passed=passed,
        failed=failed,
        coverage_pct=_parse_coverage_pct(combined_output),
        report=combined_output,
    )


def _project_host_path(config: ExecutorConfig, project_id: str) -> Path:
    """Resolves and validates project_id against the configured projects root."""
    return resolve_project_path(config.projects_host_path, project_id)


def _container_path(path: str) -> str:
    """Validates path stays within the workspace mount and returns its container path."""
    return str(resolve_project_path(Path(WORKSPACE_MOUNT_TARGET), path))


def _extract_single_file(chunks: bytes, member_name: str) -> str:
    """Extracts a single file's text content from a get_archive() tar stream."""
    buf = io.BytesIO(b"".join(chunks))
    with tarfile.open(fileobj=buf) as tar:
        member = tar.getmember(member_name)
        extracted = tar.extractfile(member)
        if extracted is None:
            raise ContainerExecutionError(f"'{member_name}' is not a regular file")
        return extracted.read().decode("utf-8")


def _build_single_file_tar(member_name: str, content: str) -> bytes:
    """Builds an in-memory tar archive containing a single file, for put_archive()."""
    data = content.encode("utf-8")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info = tarfile.TarInfo(name=member_name)
        info.size = len(data)
        info.uid = WORKSPACE_UID
        info.gid = WORKSPACE_UID
        info.mode = 0o644
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _parse_pytest_summary(output: str) -> tuple[int, int]:
    """Parses passed/failed counts from pytest's final summary line."""
    passed = 0
    failed = 0
    for match in _PYTEST_SUMMARY_RE.finditer(output):
        count = int(match.group("count"))
        outcome = match.group("outcome")
        if outcome == "passed":
            passed = count
        elif outcome in ("failed", "error", "errors"):
            failed += count
    return passed, failed


def _parse_coverage_pct(output: str) -> float | None:
    """Parses the TOTAL coverage percentage from a coverage.py term report, if present."""
    match = _COVERAGE_TOTAL_RE.search(output)
    return float(match.group(1)) if match else None
