"""Workspace container lifecycle: create an idle sandbox, exec into it, remove it.

Every container this module creates satisfies the full checklist in
.claude/rules/security.md: non-root user, all capabilities dropped,
no-new-privileges, project-scoped bind mount only, an isolated network,
explicit CPU/memory limits, and guaranteed removal in a finally block. From
Phase 17 onward a container may also get a second mount: a per-project,
per-stack named cache volume for its package manager (`pip`/`npm`) --
still zero host paths outside `projects/<id>/`, since a Docker-managed named
volume isn't a host path, and it's only ever used for that cache directory.
Which stacks get a cache volume, and which manifest/install commands run,
is driven entirely by the registry loaded from config/stacks.yaml (ADR-045)
rather than by anything hardcoded in this module.

workspace_container() calls client.containers.create() and container.start()
as two separate steps rather than the SDK's combined containers.run(): if
create() succeeds but start() then fails (observed in production as a 404
"no such container" from the proxied daemon), a single run() call raises
before ever assigning its return value, so the local `container` variable
stays None and the finally block's cleanup never finds the container Docker
already created -- it leaks, silently, forever. Assigning `container`
immediately after create() succeeds means start() failing afterward still
leaves a real container reference behind for the finally block to remove.
"""

import concurrent.futures
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import docker
from docker.errors import APIError, ImageNotFound, NotFound
from docker.models.containers import Container

from services.executor.config import ExecutorConfig, image_for_stack
from services.executor.errors import CommandTimeoutError, ContainerExecutionError
from services.executor.schemas import CommandResult
from services.executor.stacks import StackConfig

logger = logging.getLogger(__name__)

WORKSPACE_MOUNT_TARGET = "/workspace"
WORKSPACE_UID = 1000
WORKSPACE_IDLE_COMMAND = ["sleep", "infinity"]


def cache_volume_name(project_id: str, stack: str) -> str:
    """Builds the per-project, per-stack package-manager cache volume's name (ADR-028).

    Keyed by both project_id and stack -- not project_id alone -- so a
    project that is ever built with more than one stack (or whose declared
    stack changes over its lifetime) never mounts one stack's package-manager
    cache contents at another stack's cache_dir path inside the container.
    """
    return f"foundry-cache-{project_id}-{stack}"


@contextmanager
def workspace_container(
    client: docker.DockerClient,
    config: ExecutorConfig,
    project_host_path: Path,
    stack: str = "python",
    project_id: str | None = None,
    install_dependencies: bool = False,
    install_timeout: int | None = None,
) -> Iterator[Container]:
    """Starts a locked-down, idle workspace container and guarantees its removal.

    Args:
        client: A Docker SDK client, built by build_docker_client().
        config: The Executor's runtime configuration.
        project_host_path: The *host* absolute path of the project directory
            to bind-mount, read-write, as the container's only mount.
        stack: The project's declared stack -- selects both the workspace
            image (image_for_stack) and, if one is registered, a per-project
            named cache volume for that stack's package manager (ADR-028).
            A stack with no registered cache directory gets no second mount
            at all, only the project bind mount.
        project_id: The project this container belongs to, used to name its
            cache volume (`foundry-cache-{project_id}`). Defaults to
            project_host_path's final path segment, which is always the
            project_id for a path built by services.executor.paths.
        install_dependencies: If True, and stack has a registered manifest
            file (`requirements.txt`, `package.json`), checks for it inside
            the container once it starts and runs the matching install
            command before yielding -- see _install_dependencies_if_present.
            False for tool calls that don't execute project code
            (read_file/write_file/list_files), so a plain file operation
            never pays an install check's cost.
        install_timeout: Seconds to allow the manifest check/install command
            (see install_dependencies). Defaults to
            config.command_timeout_seconds. Callers with their own
            caller-supplied timeout (tool_run_command's `timeout` argument)
            must pass it here too -- otherwise the install phase silently
            runs under the *config default* regardless of what the caller
            asked for, so a caller-requested short timeout gets no
            protection during install, and a caller-requested longer
            timeout still gets capped at the default during install.

    Yields:
        The running Container, ready for exec_run/get_archive/put_archive calls.

    Raises:
        ContainerExecutionError: If the container fails to start or its
            configured image is missing.
    """
    if project_id is None:
        project_id = project_host_path.name
    if install_timeout is None:
        install_timeout = config.command_timeout_seconds

    stack_entry = config.stacks.stacks.get(stack)
    volumes = {str(project_host_path): {"bind": WORKSPACE_MOUNT_TARGET, "mode": "rw"}}
    if stack_entry is not None and stack_entry.cache_dir is not None:
        volumes[cache_volume_name(project_id, stack)] = {
            "bind": stack_entry.cache_dir, "mode": "rw",
        }

    container: Container | None = None
    try:
        try:
            image = image_for_stack(stack, config)
            logger.info(
                "project %s: creating workspace container (stack=%s, image=%s)",
                project_id, stack, image,
            )
            container = client.containers.create(
                image,
                WORKSPACE_IDLE_COMMAND,
                user=f"{WORKSPACE_UID}:{WORKSPACE_UID}",
                working_dir=WORKSPACE_MOUNT_TARGET,
                volumes=volumes,
                cap_drop=["ALL"],
                security_opt=["no-new-privileges:true"],
                network=config.network_name,
                mem_limit=config.mem_limit,
                nano_cpus=config.nano_cpus,
                pids_limit=config.pids_limit,
            )
            container.start()
            logger.info(
                "project %s: workspace container %s started", project_id, container.id[:12]
            )
        except (APIError, ImageNotFound) as exc:
            raise ContainerExecutionError(f"failed to start workspace container: {exc}") from exc
        if install_dependencies and stack_entry is not None:
            _install_dependencies_if_present(container, stack_entry, install_timeout)
        yield container
    finally:
        if container is not None:
            logger.info(
                "project %s: removing workspace container %s", project_id, container.id[:12]
            )
            try:
                container.remove(force=True)
            except APIError:
                logger.warning(
                    "failed to remove workspace container %s -- it may be orphaned",
                    container.id,
                    exc_info=True,
                )


def _install_dependencies_if_present(
    container: Container, stack_entry: StackConfig, timeout: int
) -> None:
    """Runs the stack's package-manager install command for the first manifest file found.

    Both the presence check and the install itself run *inside* the
    container, never via the Executor's own filesystem -- the Executor's own
    process has no direct access to a project's bind-mounted host path
    (ExecutorConfig.from_env's docstring), only the container being created
    does. Relies on the stack's cache volume (already mounted by
    workspace_container) for the package manager's own cache-hit behavior to
    make repeat installs fast when the manifest hasn't changed.

    A stack may have more than one recognized manifest (Python's
    `requirements.txt` and `pyproject.toml`); the first one found, checked in
    the order registered in config/stacks.yaml (ADR-045), wins -- a project
    with both is expected to be the rare exception, not the norm.

    Args:
        container: An already-running workspace container.
        stack_entry: The project's declared stack's registry entry.
        timeout: Seconds to allow each check/install command.

    Raises:
        ContainerExecutionError: If the manifest's install command exits
            non-zero. Letting a failed install pass silently would leave the
            container's subsequent command (the one the caller actually
            wanted to run) executing against missing dependencies, surfacing
            as a confusing downstream error instead of the real install
            failure.
    """
    for manifest in stack_entry.manifests:
        check = exec_in_container(container, ["test", "-f", manifest.file], timeout)
        if check.exit_code == 0:
            install_result = exec_in_container(container, manifest.install_command, timeout)
            if install_result.exit_code != 0:
                raise ContainerExecutionError(
                    f"dependency install failed for manifest '{manifest.file}': "
                    f"{install_result.stderr or install_result.stdout}"
                )
            return


def clear_project_cache_volume(
    client: docker.DockerClient,
    config: ExecutorConfig,
    project_host_path: Path,
    project_id: str,
    stack: str,
) -> None:
    """Empties a project's package-manager cache volume's contents.

    Intended to run alongside a project's other cleanup once it's archived
    or removed from the registry (TODO.md Phase 17), *before* project_id's
    directory itself is deleted from disk (this still needs project_host_path
    to exist, since it goes through the normal workspace_container() bind
    mount).

    Docker's `/volumes/*` endpoints are deliberately unreachable through
    docker-socket-proxy (`VOLUMES=0`, .claude/rules/security.md) -- actually
    deleting the named volume object would need that permission widened,
    which is exactly the kind of proxy-boundary change security.md says
    needs an ARCHITECTURE.md Decision Log entry first, not a quiet
    workaround here. Instead, this reclaims the disk space a stale cache
    volume holds by mounting it (via the same locked-down
    workspace_container() every other tool call uses) and clearing its
    contents from the inside -- a /containers/create + exec_run round trip,
    both already within the proxy's existing CONTAINERS=1/EXEC=1 allowance.
    The volume object itself stays registered with Docker, empty, ready for
    reuse if the project is ever revived.

    Args:
        client: A Docker SDK client.
        config: The Executor's runtime configuration.
        project_host_path: The project's *host* directory (workspace_container's
            bind-mount source) -- must still exist on disk when this runs.
        project_id: The project whose cache volume should be cleared.
        stack: The project's declared stack -- selects which cache directory
            (if any) to clear inside the volume. A stack with no registered
            cache directory is a no-op.
    """
    stack_entry = config.stacks.stacks.get(stack)
    if stack_entry is None or stack_entry.cache_dir is None:
        return
    cache_target = stack_entry.cache_dir
    with workspace_container(client, config, project_host_path, stack=stack, project_id=project_id) as container:
        exec_in_container(
            container, ["find", cache_target, "-mindepth", "1", "-delete"], config.command_timeout_seconds
        )


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
            still removes it afterward. Still raised even if the kill() call
            itself fails, since from the caller's perspective the command
            still didn't complete within timeout either way -- but a kill()
            failure that means "the container was already gone" (NotFound)
            is logged at debug (an expected race under load, or the
            daemon's own cleanup), while any other kill() failure is logged
            at warning with a traceback, since it may indicate a genuine
            daemon/proxy problem rather than a benign reap race.
        ContainerExecutionError: If the Docker API call itself fails.

    The worker pool backing the timeout is shut down with wait=False:
    blocking here for the pool's default wait=True would defeat the point
    of running exec_run in a worker thread in the first place -- a
    background exec_run call that doesn't unblock promptly after kill() (a
    stalled proxy/daemon HTTP call) would otherwise hang this function, and
    every caller up to the FastAPI request, well past the timeout this
    function exists to enforce.
    """
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        future = pool.submit(
            container.exec_run, command, demux=True, workdir=WORKSPACE_MOUNT_TARGET
        )
        try:
            exit_code, (stdout, stderr) = future.result(timeout=timeout)
        except concurrent.futures.TimeoutError as exc:
            try:
                container.kill()
            except NotFound as kill_exc:
                logger.debug(
                    "container %s: kill() after timeout found no such container, "
                    "already reaped: %s",
                    container.id[:12],
                    kill_exc,
                )
            except APIError as kill_exc:
                logger.warning(
                    "container %s: kill() after timeout failed: %s",
                    container.id[:12],
                    kill_exc,
                    exc_info=True,
                )
            raise CommandTimeoutError(
                f"command timed out after {timeout}s: {command!r}"
            ) from exc
        except APIError as exc:
            raise ContainerExecutionError(f"exec failed: {exc}") from exc
    finally:
        pool.shutdown(wait=False)

    return CommandResult(
        stdout=(stdout or b"").decode("utf-8", errors="replace"),
        stderr=(stderr or b"").decode("utf-8", errors="replace"),
        exit_code=exit_code,
    )
