# Event-driven Telegram progress + container/graph logging

## Problem

Two related gaps surfaced from a live deployment against a local model:

1. **Telegram progress messages are unreliable.** `run_coder_task`
   (`services/orchestrator/agents/coder.py`) streams each turn via `astream`
   and relays `chunk.content` into `progress_buffer` as it arrives. During
   tool-calling turns (the overwhelming majority of a Coder run), models emit
   little or no free-text content — the payload is in `tool_calls`, not
   `content` — so the buffer frequently stays empty for an entire task and
   the Telegram message never updates. This is the actual mechanism behind
   "sometimes it works, sometimes it's not."

2. **Errors are invisible.** When a graph node raises (observed: a local
   model returning malformed tool-call JSON deep inside LangChain's client),
   the exception propagates from `OrchestratorService`'s `_graph.invoke()`
   calls all the way to FastAPI with no project-tagged log line anywhere —
   `service.py` has no logger and no `try/except` around any of its four
   invoke sites. Separately, the Executor's `containers.py`/`tools.py` have
   almost no logging, and `api.py`'s `except ContainerExecutionError as exc:
   raise HTTPException(500, ...)` pattern is deliberate, controlled error
   handling that FastAPI does not print a traceback for — the real error
   text exists only in the HTTP response body an LLM sees as a tool result,
   never in the Executor's own container logs.

## Non-goals

- No new push channel from Orchestrator to Gateway. ADR-027 already
  established pull-only (Gateway polls `GET /stream`/`GET /status`); this
  design reuses that same already-running `ProgressPoller` loop, just
  changes what gets fed into `progress_buffer`.
- No new background watcher for the case where the Gateway's own 30-minute
  HTTP client timeout fires before the Orchestrator's synchronous graph run
  returns. Explicitly deferred by request.
- No new `LOG_LEVEL` config surface. Both services already run
  `logging.basicConfig(level=logging.INFO)`; this design adds `INFO`/
  `WARNING`/`ERROR` calls at existing thresholds, not new configurability.

## Design

### 1. Telegram messaging: event-driven, not content-streamed

Stop pushing raw `chunk.content` deltas from the `astream` loop. Instead,
push one short line to `progress_buffer.add_progress()` at each of these
points — reusing the existing pipeline (`add_progress` → `GET /stream` →
`ProgressPoller._relay`, which already runs as a background loop for a
project's whole active lifetime) with no delivery-mechanism changes:

| Event | Example line | Fired from |
|---|---|---|
| Tool dispatched | `→ write_file(app.py)` | `coder.py`'s `_build_tools` wrappers |
| Tool result (success) | `✓ wrote app.py` | same |
| Tool result (error) | `✗ write_file(app.py) failed: <error>` | same |
| Task succeeded | `✓ task succeeded (3 remaining)` | `nodes.py::node_coder`, next to its existing `logger.info` |
| Task retry | `↻ task attempt 2 failed, retrying` | same, next to existing `logger.info` |
| Task blocked | `✗ task blocked after 3 attempts` | same, next to existing `logger.warning` |
| Terminal phase | `✅ project done` / `🚫 project blocked: <reason>` | wherever phase is set to `done`/`blocked` |
| Crash | `❌ crashed: <exception message>` | `service.py`, see §2 |

Server-side logs and Telegram events stay in sync by construction, since
each new `add_progress()` call sits next to an existing (or newly added)
`logger` call at the same point.

**Idle give-up notification.** `ProgressPoller._poll_once_resilient` /
`_poll_once` currently stop polling silently once `MAX_IDLE_POLLS` is
reached — no message is ever sent. Add one final message before `_stop()`:
`⚠️ Lost track of this project — no updates in a while. Check /status or
/continue.` This is a small fix to the existing poller, not the deferred
post-timeout watcher.

### 2. Orchestrator-side crash visibility

`service.py` gains a module logger. Each of its `_graph.invoke(...)` call
sites (`handle_webhook`, `resume`, `activate`, and `activate`'s
crash-recovery continuation) is wrapped:

```python
try:
    self._graph.invoke(..., config=self._config(project_id))
except Exception:
    logger.exception("project %s: graph run crashed", project_id)
    add_progress(project_id, f"❌ crashed: {...}")
    raise
```

Re-raising leaves the existing `HTTPException(500)` / Gateway `_relay_webhook_result`
5xx handling untouched — this only adds the missing project-tagged log line
and Telegram event.

`agents/coder.py`'s three tool wrappers (`read_file`, `write_file`,
`run_command` closures in `_build_tools`) gain a module logger and
`logger.info`/`logger.warning` calls matching the new `add_progress` calls
from §1, so every tool dispatch/result is in the Orchestrator's own
container log, not just Telegram.

### 3. Executor-side container/tool visibility

- `containers.py`: `logger.info` on container create (image, stack), start
  success, and removal — full lifecycle, not just the existing
  cleanup-failure warning.
- `tools.py`: add a module logger. `logger.info` at the start of each
  `tool_*` call (project_id, operation, key args) and its outcome (exit
  code for commands, byte count for reads/writes). `logger.warning` when a
  tool raises (`ContainerExecutionError`, `CommandTimeoutError`,
  `FileNotFoundError`), with the real detail, before it becomes an HTTP
  error.
- `api.py`: in each `except ContainerExecutionError/CommandTimeoutError as
  exc:` block, add `logger.exception(...)` before `raise HTTPException(...)`,
  so the full traceback lands in the Executor's container log even though
  the HTTP response body stays a clean, generic 500.

## Testing

- `services/orchestrator/agents/tests/unit/test_coder.py`: assert
  `add_progress` (mocked/monitored) is called with the expected event
  strings for a tool dispatch, a tool success, and a tool error, using the
  existing `RecordingExecutorClient`/`FakeRouter` fixtures. Assert the old
  raw-content relay path is gone (no `add_progress` call from a
  content-only chunk).
- `services/orchestrator/graph/tests/unit/test_nodes.py`: assert
  `add_progress` fires at task success/retry/blocked transitions.
- `services/orchestrator/tests/unit/test_service.py`: a node that raises
  inside a `FakeRouter`-scripted run causes `service.handle_webhook`/
  `resume`/`activate` to still raise (unchanged), but now also
  asserts a `❌ crashed:` chunk landed in `progress_buffer` for that
  project_id. Use `caplog` to assert a `project %s: graph run crashed` log
  record at ERROR level.
- `services/telegram_gateway/tests/unit/test_progress_poller.py` (existing
  or new): assert that hitting `max_idle_polls` sends exactly one final
  "lost track" message before the loop stops.
- `services/executor/tests/unit/test_containers_mocked.py` /
  `test_tools_mocked.py`: assert the new `logger.info`/`logger.warning`
  calls fire at the right points, via `caplog`, alongside existing
  mocked-Docker-client assertions.
- `services/executor/tests/unit/test_api.py` (existing): assert a
  `ContainerExecutionError` raised from a tool call produces both the
  expected `HTTPException(500)` *and* a `caplog` ERROR record with the
  original exception text.

No new integration-tier tests are needed — this design touches logging and
message content, not container/network/filesystem boundaries already
covered by `tests/integration/test_executor_sandbox.py`.
