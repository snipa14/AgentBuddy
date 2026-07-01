"""Workspace container lifecycle: create an idle sandbox, exec into it, remove it.

Every container this module creates satisfies the full checklist in
.claude/rules/security.md: non-root user, all capabilities dropped,
no-new-privileges, project-scoped bind mount only, an isolated network,
explicit CPU/memory limits, and guaranteed removal in a finally block.
"""

import concurrent.futures
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import docker
from docker.errors import APIError, ImageNotFound
from docker.models.containers import Container

from services.executor.config import ExecutorConfig
from services.executor.errors import CommandTimeoutError, ContainerExecutionError
from services.executor.schemas import CommandResult

WORKSPACE_MOUNT_TARGET = "/workspace"
WORKSPACE_UID = 1000
WORKSPACE_IDLE_COMMAND = ["sleep", "infinity"]


@contextmanager
def workspace_container(
    client: docker.DockerClient,
    config: ExecutorConfig,
    project_host_path: Path,
) -> Iterator[Container]:
    """Starts a locked-down, idle workspace container and guarantees its removal.

    Args:
        client: A Docker SDK client, built by build_docker_client().
        config: The Executor's runtime configuration.
        project_host_path: The *host* absolute path of the project directory
            to bind-mount, read-write, as the container's only mount.

    Yields:
        The running Container, ready for exec_run/get_archive/put_archive calls.

    Raises:
        ContainerExecutionError: If the container fails to start or its
            configured image is missing.
    """
    container: Container | None = None
    try:
        try:
            container = client.containers.run(
                config.workspace_image,
                WORKSPACE_IDLE_COMMAND,
                detach=True,
                user=f"{WORKSPACE_UID}:{WORKSPACE_UID}",
                working_dir=WORKSPACE_MOUNT_TARGET,
                volumes={
                    str(project_host_path): {"bind": WORKSPACE_MOUNT_TARGET, "mode": "rw"}
                },
                cap_drop=["ALL"],
                security_opt=["no-new-privileges:true"],
                network=config.network_name,
                mem_limit=config.mem_limit,
                nano_cpus=config.nano_cpus,
            )
        except (APIError, ImageNotFound) as exc:
            raise ContainerExecutionError(f"failed to start workspace container: {exc}") from exc
        yield container
    finally:
        if container is not None:
            try:
                container.remove(force=True)
            except APIError:
                pass


def exec_in_container(container: Container, command: list[str], timeout: int) -> CommandResult:
    """Runs a single command inside an already-running workspace container.

    Args:
        container: A container yielded by workspace_container().
        command: The command to run, as an argv list.
        timeout: Seconds to wait before killing the container and giving up.

    Returns:
        A CommandResult with stdout, stderr, and exit_code.

    Raises:
        CommandTimeoutError: If the command exceeds timeout. The container is
            killed so the blocked exec call unblocks; workspace_container()
            still removes it afterward.
        ContainerExecutionError: If the Docker API call itself fails.
    """
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            container.exec_run, command, demux=True, workdir=WORKSPACE_MOUNT_TARGET
        )
        try:
            exit_code, (stdout, stderr) = future.result(timeout=timeout)
        except concurrent.futures.TimeoutError as exc:
            container.kill()
            raise CommandTimeoutError(f"command timed out after {timeout}s: {command!r}") from exc
        except APIError as exc:
            raise ContainerExecutionError(f"exec failed: {exc}") from exc

    return CommandResult(
        stdout=(stdout or b"").decode("utf-8", errors="replace"),
        stderr=(stderr or b"").decode("utf-8", errors="replace"),
        exit_code=exit_code,
    )
