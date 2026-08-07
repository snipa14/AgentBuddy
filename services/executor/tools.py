"""The Executor's typed tool functions, per ARCHITECTURE.md §4.3.

Every function here is the only sanctioned way an agent tool call can touch
a project's files or run a command -- always inside a freshly created,
locked-down workspace container (services.executor.containers), never via
direct filesystem access from the Executor's own process.
"""

import io
import json
import logging
import os
import re
import tarfile
from pathlib import Path

import docker
from docker.errors import APIError, NotFound

from services.executor.config import ExecutorConfig
from services.executor.containers import (
    WORKSPACE_MOUNT_TARGET,
    WORKSPACE_UID,
    clear_project_cache_volume,
    exec_in_container,
    workspace_container,
)
from services.executor.errors import ContainerExecutionError
from services.executor.paths import resolve_project_path
from services.executor.schemas import CommandResult, FileEntry, ScreenshotResult, TestResult

_PYTEST_SUMMARY_RE = re.compile(r"(?P<count>\d+)\s+(?P<outcome>passed|failed|error|errors)")
_COVERAGE_TOTAL_RE = re.compile(r"^TOTAL\s+.*\s(\d+)%\s*$", re.MULTILINE)

logger = logging.getLogger(__name__)

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
    stack: str = "python",
) -> CommandResult:
    """Runs an arbitrary command inside a fresh workspace container for project_id.

    Args:
        client: A Docker SDK client.
        config: The Executor's runtime configuration.
        project_id: The project whose directory gets bind-mounted.
        command: The command to run, as an argv list (never a shell string).
        timeout: Seconds to allow before killing the container. Defaults to
            config.command_timeout_seconds.
        stack: The project's declared stack -- selects the workspace image
            and cache volume (ADR-028); also runs that stack's dependency
            manifest check/install before *command* executes.

    Returns:
        A CommandResult with stdout, stderr, and exit_code.
    """
    effective_timeout = timeout if timeout is not None else config.command_timeout_seconds
    logger.info("project %s: run_command(%s)", project_id, command)
    project_host_path = _project_host_path(config, project_id)
    with workspace_container(
        client, config, project_host_path, stack=stack, project_id=project_id, install_dependencies=True
    ) as container:
        result = exec_in_container(container, command, effective_timeout)
    logger.info("project %s: run_command exit_code=%d", project_id, result.exit_code)
    return result


def tool_read_file(
    client: docker.DockerClient,
    config: ExecutorConfig,
    project_id: str,
    path: str,
    stack: str = "python",
) -> str:
    """Reads a UTF-8 text file from within project_id's directory.

    Args:
        client: A Docker SDK client.
        config: The Executor's runtime configuration.
        project_id: The project whose directory gets bind-mounted.
        path: A path relative to the project's root.
        stack: The project's declared stack -- selects the workspace image.

    Returns:
        The file's decoded content.

    Raises:
        PathTraversalError: If path resolves outside the project root.
        FileNotFoundError: If path doesn't exist in the project.
    """
    logger.info("project %s: read_file(%s)", project_id, path)
    container_path = _container_path(path)
    project_host_path = _project_host_path(config, project_id)
    with workspace_container(client, config, project_host_path, stack=stack, project_id=project_id) as container:
        try:
            chunks, _ = container.get_archive(container_path)
        except NotFound as exc:
            logger.warning("project %s: read_file(%s) not found", project_id, path)
            raise FileNotFoundError(f"'{path}' not found in project '{project_id}'") from exc
        content = _extract_single_file(chunks, os.path.basename(container_path))
        logger.info("project %s: read_file(%s) returned %d bytes", project_id, path, len(content))
        return content


def tool_write_file(
    client: docker.DockerClient,
    config: ExecutorConfig,
    project_id: str,
    path: str,
    content: str,
    stack: str = "python",
) -> bool:
    """Writes a UTF-8 text file into project_id's directory, creating parents as needed.

    Args:
        client: A Docker SDK client.
        config: The Executor's runtime configuration.
        project_id: The project whose directory gets bind-mounted.
        path: A path relative to the project's root.
        content: The text to write.
        stack: The project's declared stack -- selects the workspace image.

    Returns:
        True once the write has landed in the bind-mounted project directory.

    Raises:
        PathTraversalError: If path resolves outside the project root.
        ContainerExecutionError: If mkdir or the file write itself fails
            inside the container.
    """
    logger.info("project %s: write_file(%s), %d bytes", project_id, path, len(content))
    container_path = _container_path(path)
    directory = os.path.dirname(container_path)
    project_host_path = _project_host_path(config, project_id)
    with workspace_container(
        client, config, project_host_path, stack=stack, project_id=project_id
    ) as container:
        mkdir_result = exec_in_container(
            container, ["mkdir", "-p", directory], config.command_timeout_seconds
        )
        if mkdir_result.exit_code != 0:
            logger.warning(
                "project %s: write_file(%s) mkdir failed: %s",
                project_id, path, mkdir_result.stderr,
            )
            raise ContainerExecutionError(
                f"mkdir failed for '{path}': {mkdir_result.stderr}"
            )
        try:
            container.put_archive(
                directory, _build_single_file_tar(os.path.basename(container_path), content)
            )
        except APIError as exc:
            logger.warning(
                "project %s: write_file(%s) put_archive failed: %s", project_id, path, exc
            )
            raise ContainerExecutionError(f"write failed for '{path}': {exc}") from exc
    return True


def tool_list_files(
    client: docker.DockerClient,
    config: ExecutorConfig,
    project_id: str,
    path: str = ".",
    stack: str = "python",
) -> list[FileEntry]:
    """Lists the immediate entries of a directory within project_id's directory.

    Args:
        client: A Docker SDK client.
        config: The Executor's runtime configuration.
        project_id: The project whose directory gets bind-mounted.
        path: A directory path relative to the project's root.
        stack: The project's declared stack -- selects the workspace image.

    Returns:
        A FileEntry per immediate child of path.

    Raises:
        PathTraversalError: If path resolves outside the project root.
        ContainerExecutionError: If listing the directory fails.
    """
    container_path = _container_path(path)
    project_host_path = _project_host_path(config, project_id)
    with workspace_container(client, config, project_host_path, stack=stack, project_id=project_id) as container:
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
    stack: str = "python",
) -> TestResult:
    """Runs a project's test suite with coverage inside a fresh workspace container.

    Args:
        client: A Docker SDK client.
        config: The Executor's runtime configuration.
        project_id: The project whose directory gets bind-mounted.
        framework: The legacy fallback test command selector, used only when
            stack has no registered test_command in config.stacks: "pytest"
            (default, coverage-parsed) or "npm" (runs `npm test`).
        stack: The project's declared stack -- selects the workspace image
            and cache volume (ADR-028) and, when registered, the test
            command to run (ADR-045); also runs that stack's dependency
            manifest check/install before the test command executes.

    Returns:
        A TestResult with passed/failed counts, coverage percentage (if
        parseable from the report -- coverage.py output only), and the raw
        combined output.

    Raises:
        ValueError: If stack has no registered test_command and framework
            isn't "pytest" or "npm".
    """
    entry = config.stacks.stacks.get(stack)
    if entry is not None and entry.test_command is not None:
        command = entry.test_command
    elif framework == "pytest":
        command = ["python3", "-m", "pytest", "--cov=.", "--cov-report=term-missing", "-q"]
    elif framework == "npm":
        command = ["npm", "test"]
    else:
        raise ValueError(f"unsupported test framework: {framework!r}")

    project_host_path = _project_host_path(config, project_id)
    with workspace_container(
        client, config, project_host_path, stack=stack, project_id=project_id, install_dependencies=True
    ) as container:
        result = exec_in_container(container, command, config.command_timeout_seconds)

    combined_output = f"{result.stdout}\n{result.stderr}"
    passed, failed = _parse_pytest_summary(combined_output)
    return TestResult(
        passed=passed,
        failed=failed,
        coverage_pct=_parse_coverage_pct(combined_output),
        report=combined_output,
    )


def tool_clear_project_cache(
    client: docker.DockerClient,
    config: ExecutorConfig,
    project_id: str,
    stack: str,
) -> bool:
    """Empties a project's package-manager cache volume's contents.

    Intended for use once a project is archived or removed from the
    registry -- call this *before* deleting project_id's own directory from
    disk (see containers.clear_project_cache_volume's docstring for why the
    Docker volume object itself isn't deleted outright).

    Args:
        client: A Docker SDK client.
        config: The Executor's runtime configuration.
        project_id: The project whose cache volume should be cleared.
        stack: The project's declared stack.

    Returns:
        True once the clear has completed (or was a no-op for a stack with
        no registered cache directory).
    """
    project_host_path = _project_host_path(config, project_id)
    clear_project_cache_volume(client, config, project_host_path, project_id, stack)
    return True


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


_TOOL_TAKE_SCREENSHOT_PY = r"""
import base64, subprocess, sys, time, urllib.request

url = sys.argv[1]
project_dir = sys.argv[2] if len(sys.argv) > 2 else ""
auto_start = sys.argv[3].lower() == "true" if len(sys.argv) > 3 else False
breakpoint_w = int(sys.argv[4]) if len(sys.argv) > 4 else 1280

def wait_for_url(max_wait=30):
    deadline = time.time() + max_wait
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=2)
            return True
        except Exception:
            time.sleep(0.5)
    return False

server_proc = None
if auto_start and project_dir:
    scripts = [
        ("npm", ["run", "dev"]),
        ("npx", ["vite", "--port", "5173"]),
        ("npx", ["next", "dev", "-p", "3000"]),
        ("python3", ["-m", "http.server", "8000"]),
    ]
    for exe, cmd in scripts:
        try:
            server_proc = subprocess.Popen(
                [exe] + cmd, cwd=project_dir,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            if wait_for_url():
                break
            server_proc.terminate()
            server_proc.wait()
            server_proc = None
        except FileNotFoundError:
            continue

try:
    ok = wait_for_url(15)
    if not ok:
        print("ERROR:url_not_ready", file=sys.stderr)
        sys.exit(1)

    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
        page = browser.new_page(viewport={"width": breakpoint_w, "height": 900})
        try:
            page.goto(url, wait_until="networkidle", timeout=30000)
            png_bytes = page.screenshot(full_page=True)
        finally:
            browser.close()

    print(base64.b64encode(png_bytes).decode())
except ImportError:
    print("ERROR:no_playwright", file=sys.stderr)
    sys.exit(1)
except Exception as exc:
    print(f"ERROR:{exc}", file=sys.stderr)
    sys.exit(1)
finally:
    if server_proc and server_proc.poll() is None:
        server_proc.terminate()
        try:
            server_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server_proc.kill()
            server_proc.wait()
"""


def tool_take_screenshot(
    client: docker.DockerClient,
    config: ExecutorConfig,
    project_id: str,
    url: str = "http://localhost:5173",
    auto_start_server: bool = False,
    breakpoint_width: int = 1280,
    stack: str = "python",
) -> ScreenshotResult:
    """Captures a screenshot of a web page running inside project_id's container.

    Launches headless Chromium via Playwright inside the workspace container
    (ADR-024), navigates to *url* at a viewport of *breakpoint_width* pixels
    wide, and returns a full-page screenshot as a base64-encoded PNG. If
    *auto_start_server* is True and no server is yet listening on *url*,
    attempts to start one from project_dir using common dev-server commands
    (npm run dev, npx vite, etc.) first.

    Args:
        client: A Docker SDK client.
        config: The Executor's runtime configuration.
        project_id: The project whose directory gets bind-mounted.
        url: The URL to capture (e.g. http://localhost:5173).
        auto_start_server: Whether to attempt starting a dev server if none is
            listening on *url*.
        breakpoint_width: Target viewport width for responsive sizing.
        stack: The project's declared stack -- selects the workspace image
            and cache volume (ADR-028); also runs that stack's dependency
            manifest check/install before the dev server is started, so
            auto_start_server has installed dependencies to work with.

    Returns:
        A ScreenshotResult with base64-encoded PNG image data, or an error
        message if the capture failed (e.g. Playwright/Chromium not
        installed, or the page never became reachable).
    """
    project_host_path = _project_host_path(config, project_id)
    auto_start_flag = str(auto_start_server).lower()

    command = [
        "python3", "-c", _TOOL_TAKE_SCREENSHOT_PY,
        url, WORKSPACE_MOUNT_TARGET, auto_start_flag, str(breakpoint_width),
    ]

    with workspace_container(
        client, config, project_host_path, stack=stack, project_id=project_id, install_dependencies=True
    ) as container:
        result = exec_in_container(container, command, config.command_timeout_seconds)

    if result.exit_code == 0 and result.stdout.strip():
        return ScreenshotResult(image_b64=result.stdout.strip())

    error_msg = result.stderr.strip() or result.stdout.strip() or "screenshot failed"
    return ScreenshotResult(image_b64="", error=error_msg)
