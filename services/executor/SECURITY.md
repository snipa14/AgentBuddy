# Executor Security Model

The Executor is the only Foundry service allowed to talk to the Docker
control surface (see `.claude/rules/security.md` and ARCHITECTURE.md §4.3).
This document is the threat model for that boundary: what it's designed to
prevent, how, and what's accepted as residual risk for v1.

## What's prevented, and how

| Threat | Mitigation |
|---|---|
| An agent gets a host shell | Agents only ever call `tool_run_command`/`tool_read_file`/`tool_write_file`/`tool_list_files`/`tool_run_tests`. None of them accept or construct shell strings -- `tool_run_command` takes an argv list, passed straight to `exec_run`, never through `sh -c`. |
| An agent reaches the raw Docker socket | The Executor never mounts `/var/run/docker.sock` into a workspace container, and never exposes its own `DOCKER_HOST` to one. `services/executor/tests/integration/test_executor_sandbox.py::test_run_command_cannot_reach_the_docker_socket` asserts the path doesn't exist inside the sandbox. |
| An agent reads/writes another project's files | Every workspace container gets exactly one bind mount: `<projects_host_path>/<project_id>` -> `/workspace`. No other host path is ever mounted. `test_run_command_cannot_read_a_sibling_projects_files` proves a path-traversal attempt from inside `/workspace` can't reach a sibling project's directory, because that directory was never mounted into this container at all. |
| An agent escapes `/workspace` via `tool_read_file`/`tool_write_file`/`tool_list_files` | `services/executor/paths.py::resolve_project_path` rejects any `path` argument (`..` segments, absolute paths) that would resolve outside the project root, before any Docker call is made. Raises `PathTraversalError`, a `ValueError` subclass. |
| A container escalates privilege inside itself | Every workspace container runs as UID 1000 (never root), with `cap_drop=["ALL"]` and `security_opt=["no-new-privileges:true"]`. `images/workspace-python/Dockerfile` additionally strips setuid/setgid bits from the base image at build time, as defense in depth beyond the runtime flags. |
| A container exhausts host CPU/memory | Every container gets an explicit `mem_limit` and `nano_cpus` (configurable via `ExecutorConfig`, not removable). `test_run_command_is_capped_by_the_configured_memory_limit` proves an over-limit allocation is killed. |
| A container pivots to core services (Orchestrator, docker-socket-proxy, Telegram Gateway) | Workspace containers attach only to `foundry_workspace`, a network isolated from `foundry_internal` (where the core services and the proxy live) -- never `host` networking, always an explicit `--network`. |
| A crashed or timed-out task leaves an orphaned container | `services/executor/containers.py::workspace_container` is a context manager whose `finally` block calls `container.remove(force=True)` unconditionally. `exec_in_container` runs the exec call in a background thread with a timeout; on timeout it kills the container (unblocking the exec call) before the context manager removes it. `test_command_exceeding_timeout_raises_and_leaves_no_orphaned_container` proves no container survives a timeout. |
| The Executor's own Docker calls exceed the whitelisted proxy scope | `docker-socket-proxy` is configured with `CONTAINERS=1 EXEC=1 IMAGES=1 POST=1` and everything else `0` (`BUILD`, `NETWORKS`, `VOLUMES`, `SWARM`, `SYSTEM`, ...). The Executor's own code never calls network- or volume-mutating APIs -- it only ever references the pre-provisioned `foundry_workspace` network by name, since the proxy would deny a `NETWORKS`-scoped call outright. |

## The Docker-outside-of-Docker (DooD) detail

The Executor's own container does **not** have `projects/` bind-mounted
(only the Orchestrator does, per `docker-compose.yml`). When the Executor
asks the Docker daemon (via the proxy) to bind-mount a project directory
into a workspace container, that mount source is resolved by the daemon
against the **host** filesystem, not the Executor's own container
filesystem -- a bind-mount source path that only exists inside the
Executor's container would resolve to nothing (or the wrong thing) on the
host running the actual daemon.

`PROJECTS_HOST_PATH` (an operator-set environment variable, read once at
Executor startup via `ExecutorConfig.from_env()`) is the absolute host path
of the `projects/` directory. It is never derived from, or influenced by,
agent/LLM input -- it's deployment configuration, set the same way
`OLLAMA_ENABLED` is (ADR-015).

## Accepted residual risk

- **No enforced egress allowlist yet.** `foundry_workspace` isolates
  workspace containers from the core services network and from `host`
  networking, but it does not yet restrict *outbound* traffic to package
  registries only, as ARCHITECTURE.md §4.3 describes as the eventual goal.
  A compromised dependency installed inside a workspace container could
  still reach arbitrary internet hosts for the container's lifetime.
  A real allowlist needs a forward-proxy component (or NAT/iptables rules)
  that isn't built yet; revisit in Phase 18 (Security Hardening Pass) or
  as a dedicated future item.
- **`put_archive`/`get_archive` bypass the container's own user permissions.**
  These Docker API calls write/read a container's filesystem directly via
  the daemon, not through the sandboxed UID 1000 process. `tool_write_file`
  explicitly sets `uid=1000, gid=1000` on every file it writes so a
  subsequent `tool_run_command` (running as UID 1000) can still read/modify
  it, but this is a manual convention in `tools.py`, not something Docker
  enforces on our behalf.
- **A compromised Executor process is a high-value target.** Its Docker API
  access (even scoped through the proxy's `CONTAINERS/EXEC/IMAGES/POST`
  whitelist) is enough to create and exec into arbitrary containers on the
  host. The Executor's own code is deliberately minimal and narrow (five
  typed functions, no arbitrary code evaluation, no shell string
  construction) to keep its own attack surface small, but it is not
  independently sandboxed beyond being a normal container itself.
- **Coverage/test-result parsing is regex-based**, not structured
  (`tool_run_tests` greps pytest's text summary and coverage.py's terminal
  report). This is a correctness fragility, not a security one -- a change
  in pytest's or coverage.py's output format could make `passed`/`failed`/
  `coverage_pct` silently wrong rather than the call failing outright.

## Escape testing

`services/executor/tests/integration/test_executor_sandbox.py` is the
enforcement mechanism for the containment claims above, against a real
Docker daemon (`.claude/rules/security.md` explicitly forbids relying on a
mocked `docker.from_env()` for this). It covers:

- `write_file` -> host-visible round trip
- `read_file` returns content written directly on the host
- a workspace container cannot read a sibling project's files via path
  traversal from inside `/workspace`
- a workspace container cannot see `/var/run/docker.sock`
- an over-`mem_limit` allocation is killed
- a command exceeding its timeout raises `CommandTimeoutError` and leaves
  no orphaned container

Run it with `pytest -m integration services/executor/tests/integration`.
