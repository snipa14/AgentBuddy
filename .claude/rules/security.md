---
description: Docker security boundary, executor constraints, and host-isolation rules. Enforced whenever working in services/executor/ or on any code that touches Docker.
---

# Security Boundary Rules

## The one rule that overrides everything else

**The Executor is the only service in this codebase that may import or call the Docker SDK.** No other service — not the Orchestrator, not the Telegram Gateway, not any agent module — may create, start, stop, inspect, or remove containers, or talk to docker-socket-proxy directly. If implementing a feature that seems to require Docker access in another service, stop and redesign.

## docker-socket-proxy

The proxy whitelist is intentionally narrow. Do not widen it. The allowed proxy env vars are exactly:

```
CONTAINERS=1  EXEC=1  IMAGES=1  POST=1
```

Everything else (`BUILD`, `NETWORKS`, `VOLUMES`, `SWARM`, `SYSTEM`, `SERVICES`, `TASKS`, `CONFIGS`, `SECRETS`, `PLUGINS`, `DISTRIBUTION`) stays `0`. If a feature genuinely needs a capability not in this list, it requires an ARCHITECTURE.md Decision Log entry before the proxy config changes.

## Workspace container launch checklist

Every container spawned by the Executor must have all of the following — no exceptions, no project-level overrides:

- Non-root user (UID ≠ 0)
- `cap_drop=["ALL"]`
- `security_opt=["no-new-privileges:true"]`
- Only the target project directory bind-mounted (`projects/<id>/`), read-write. No host paths outside this.
- Isolated bridge network with an explicit `--network` argument. Never `host` networking.
- `mem_limit` set (default: `2g`, configurable per project but not removable)
- `nano_cpus` set (default: `2e9`, configurable per project but not removable)
- The container is removed (`auto_remove=True` or explicit `remove()` in `finally`) after the task completes regardless of outcome.

## What agents never get

These things must never be exposed to any LLM tool call, even indirectly:

- Raw docker socket path or the proxy's internal URL
- GitHub token, Telegram token, Claude API key (they flow through Docker secrets to the services that need them, not through the graph state)
- Host filesystem paths outside `projects/`
- The ability to modify the Docker network topology

## Escape testing

When adding new Executor functionality, include a negative test that proves the boundary holds:
- Attempt a disallowed proxy endpoint and assert the call raises (not silently fails).
- Attempt a path traversal in `tool_read_file` or `tool_write_file` (e.g., `../../etc/passwd`) and assert it raises `ValueError`, not that it just returns empty.

## The mock that proves nothing

`unittest.mock.patch("docker.from_env")` in a unit test is fine for testing Executor *logic*, but it does not prove the boundary holds. The security tests in `tests/integration/test_executor_sandbox.py` use a real Docker daemon. Do not remove or skip those tests.
