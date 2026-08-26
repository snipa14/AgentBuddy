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
| A container exhausts host CPU/memory/process-count | Every container gets an explicit `mem_limit`, `nano_cpus`, and `pids_limit` (configurable via `ExecutorConfig`, not removable). `test_run_command_is_capped_by_the_configured_memory_limit` proves an over-limit memory allocation is killed; `test_run_command_fork_bomb_is_capped_and_leaves_no_orphaned_container` proves a fork bomb is capped and the container still gets cleaned up. |
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
- a fork bomb is capped by `pids_limit` and the container still gets
  cleaned up
- `docker-socket-proxy`'s whitelist holds in both directions: `BUILD`/
  `NETWORKS` return 403, `CONTAINERS` still works (`test_proxy_boundary.py`,
  run separately from `test_executor_sandbox.py`)
- file contents read via `tool_run_command` never reach the Executor's own
  log stream, only `project_id`/command/path

Run it with `pytest -m integration services/executor/tests/integration`.

## Phase 21 hardening pass findings

Deliberate attempts to break every boundary claim above, against a real
Docker daemon:

- **Path traversal out of the bind mount** -- already covered before this
  pass, at both layers: `services/executor/tests/unit/test_paths.py`
  (`resolve_project_path` rejects `..` segments and absolute-path escapes
  before any Docker call happens) and
  `test_run_command_cannot_read_a_sibling_projects_files` (a live
  container genuinely cannot see a sibling project's directory, because
  it was never mounted). Re-run and confirmed still green on this pass.
- **Reaching docker.sock or a denied proxy endpoint directly** --
  `test_run_command_cannot_reach_the_docker_socket` (already existing)
  confirms a workspace container has no access to the raw socket.
  `test_proxy_boundary.py` (new this pass) goes further and hits the real
  docker-socket-proxy service directly: confirmed live against the real
  docker-socket-proxy service from docker-compose.yml (not a mock):
  `test_proxy_boundary.py` denies both a `NETWORKS` list call and a `BUILD`
  image-build call with HTTP 403 through the proxy, while the whitelisted
  `CONTAINERS` list call still succeeds -- proving the whitelist holds in
  both directions, not just that everything happens to fail. This sandbox
  has no `docker` CLI binary, so the test's fixtures use the Docker Python
  SDK directly, parsing the real `docker-compose.yml`'s `docker-socket-proxy`
  service block (image, environment, volumes) at test-run time rather than
  hardcoding a second copy of the whitelist, so it stays accurate if that
  file ever changes. One separate, real finding surfaced while building this
  test: `docker-compose.yml`'s `foundry_internal` network has no explicit
  `name:` override (unlike `foundry_workspace`, which already pins one for
  exactly this reason), so under a real `docker compose up` with this repo's
  `COMPOSE_PROJECT_NAME=foundry` (`.env`), Compose would create it as
  `foundry_foundry_internal`, not literally `foundry_internal`. This doesn't
  affect this test itself (its SDK-fallback path defines and uses its own
  literal name, self-consistently), but is a real, pre-existing
  `docker-compose.yml` gap worth its own follow-up fix: pin `name:
  foundry_internal` on that network the same way `foundry_workspace` already
  does.
- **Fork bomb / resource exhaustion** -- found a real gap: every other
  per-container resource (memory via `mem_limit`, CPU via `nano_cpus`,
  capabilities via `cap_drop`) was capped, but process count was not. A
  fork bomb was bounded by memory/CPU limits but could still exhaust the
  **host's** shared PID table before either engaged. Fixed by adding
  `pids_limit` (default 256, `WORKSPACE_PIDS_LIMIT`-configurable) to every
  workspace container alongside the existing caps.
  `test_run_command_fork_bomb_is_capped_and_leaves_no_orphaned_container`
  proves the fix and that container cleanup still holds under this
  specific abuse pattern.
- **Secrets in logs** -- audited every logging call in
  `services/executor/tools.py`: all of them log `project_id`,
  `path`/`command`, or an exception's type -- never file contents or a
  command's captured output.
  `test_run_command_never_logs_literal_file_contents` makes this a
  permanent regression test rather than a one-time grep.
