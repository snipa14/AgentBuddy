# Event-driven Telegram progress + container/graph logging Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the unreliable content-streamed Telegram progress relay with discrete tool/task/terminal/crash events, and add logging across the Orchestrator and Executor so a crashed graph run or failed container operation is visible in `docker compose logs`, not silently lost.

**Architecture:** No new services, processes, or push channels. Reuses the existing pull-based `progress_buffer` → `GET /stream` → `ProgressPoller` pipeline (ADR-027) for tool-call and task-boundary events, adds a `logger`-backed crash wrapper around every `OrchestratorService._graph.invoke()` call site, and adds `logging.info`/`warning`/`exception` calls through the Executor's container lifecycle and tool functions.

**Tech Stack:** Python 3.12, `logging` stdlib, existing `progress_buffer`/`ProgressPoller`/LangGraph/FastAPI/aiogram stack already in the repo. No new dependencies.

## Global Constraints

- No inline comments beyond what `.claude/rules/security.md`-style codebase already tolerates — this codebase's convention (`CLAUDE.md`) is self-documenting code with docstrings, not inline `#` comments, except for non-obvious workarounds with a GitHub issue reference. None of these tasks need one.
- No bare `except`. Every new `except` clause names a specific exception type, or `Exception` paired with a log call (`.claude/rules/testing.md`, `CLAUDE.md`).
- Type hints everywhere; no `Any` without a comment explaining why (this plan introduces no new `Any` usage).
- Unit tests only (`services/<name>/tests/unit`) — no real Docker/network/filesystem. No integration-tier tests are needed; this work touches logging and message content, not the container/network boundary already covered by `tests/integration/test_executor_sandbox.py`.
- Test naming: `test_<thing_under_test>_<condition>_<expected_outcome>` (`.claude/rules/testing.md`).
- Commit directly to `release/v1` after each task (established pattern for this session/repo — no feature branch, no worktree), Conventional Commits format (`.claude/rules/git.md`).
- **Deviation from the committed spec** (`docs/superpowers/specs/2026-08-05-telegram-progress-and-logging-design.md`): the spec's event table lists the terminal-phase event (`✅ project done` / `🚫 project blocked`) as flowing through `progress_buffer` like the other events. While writing this plan, tracing the exact call order revealed a race: `OrchestratorService.status()` clears a project's `progress_buffer` the moment it observes a terminal phase (existing behavior, `service.py:205-210`), and `handle_webhook`/`resume` already call `self.status(project_id)` internally, synchronously, immediately after the same invoke that produces the transition — before any concurrent poller HTTP request could possibly read the chunk. Pushing the terminal event through `progress_buffer` would make it invisible to the poller essentially always, not just occasionally. Task 5 below instead has `ProgressPoller` emit the terminal message directly from the `phase` field it already reads via `GET /status` every cycle (a field never subject to that clearing race), which delivers the exact same user-visible behavior the spec asked for. The crash event is unaffected by this and still flows through `progress_buffer` exactly as specced, since a crash never flips `phase` into a terminal value and so `clear_progress` never fires near it.

---

## File Structure

| File | Change |
|---|---|
| `services/executor/containers.py` | Modify — INFO logs for container create/start/remove lifecycle |
| `services/executor/tools.py` | Modify — module logger; INFO on each tool call's start/outcome, WARNING on failure |
| `services/executor/api.py` | Modify — `logger.exception(...)` before each `HTTPException` conversion |
| `services/executor/tests/unit/test_containers_mocked.py` | Modify — assert new log records via `caplog` |
| `services/executor/tests/unit/test_tools_mocked.py` | Modify — assert new log records via `caplog` |
| `services/executor/tests/unit/test_api.py` | Modify — assert a log record accompanies each 500 |
| `services/orchestrator/service.py` | Modify — `_invoke_graph` helper wrapping all 5 `_graph.invoke()` call sites |
| `services/orchestrator/tests/unit/test_service.py` | Modify — crash logging + progress-event test |
| `services/orchestrator/agents/coder.py` | Modify — remove raw content streaming; add tool-call dispatch/result logging + progress events |
| `services/orchestrator/agents/tests/unit/test_coder.py` | Modify — replace the content-streaming test with tool-event tests |
| `services/orchestrator/graph/nodes.py` | Modify — task success/retry/blocked progress events next to existing log calls |
| `services/orchestrator/graph/tests/unit/test_nodes.py` | Modify — assert the new progress events |
| `services/telegram_gateway/progress_poller.py` | Modify — terminal-phase and idle-give-up final messages |
| `services/telegram_gateway/tests/unit/test_progress_poller.py` | Modify — assert the new final messages |

---

### Task 1: Executor logging (container lifecycle, tool calls, HTTP error path)

**Files:**
- Modify: `services/executor/containers.py`
- Modify: `services/executor/tools.py`
- Modify: `services/executor/api.py`
- Test: `services/executor/tests/unit/test_containers_mocked.py`
- Test: `services/executor/tests/unit/test_tools_mocked.py`
- Test: `services/executor/tests/unit/test_api.py`

**Interfaces:**
- Consumes: nothing from other tasks — this task is self-contained within `services/executor/`.
- Produces: nothing other tasks depend on (the Orchestrator only talks to the Executor over HTTP, whose response shapes are unchanged).

- [ ] **Step 1: Write the failing tests for container lifecycle logging**

Add to `services/executor/tests/unit/test_containers_mocked.py` (after the existing imports, add `import logging`):

```python
def test_workspace_container_logs_create_start_and_remove(config, tmp_path, caplog):
    mock_container = MagicMock(spec=Container)
    mock_container.id = "abc123def456"
    client = MagicMock()
    client.containers.create.return_value = mock_container

    with caplog.at_level(logging.INFO):
        with workspace_container(client, config, tmp_path, stack="python", project_id="proj-1"):
            pass

    messages = [record.message for record in caplog.records]
    assert any("proj-1" in m and "creating workspace container" in m for m in messages)
    assert any("proj-1" in m and "abc123def456"[:12] in m and "started" in m for m in messages)
    assert any("proj-1" in m and "abc123def456"[:12] in m and "removing" in m for m in messages)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest services/executor/tests/unit/test_containers_mocked.py::test_workspace_container_logs_create_start_and_remove -v`
Expected: FAIL — no log records match (the log calls don't exist yet).

- [ ] **Step 3: Add the lifecycle logging to `workspace_container`**

In `services/executor/containers.py`, the current body (lines 103-135) is:

```python
    container: Container | None = None
    try:
        try:
            container = client.containers.create(
                image_for_stack(stack, config),
                WORKSPACE_IDLE_COMMAND,
                user=f"{WORKSPACE_UID}:{WORKSPACE_UID}",
                working_dir=WORKSPACE_MOUNT_TARGET,
                volumes=volumes,
                cap_drop=["ALL"],
                security_opt=["no-new-privileges:true"],
                network=config.network_name,
                mem_limit=config.mem_limit,
                nano_cpus=config.nano_cpus,
            )
            container.start()
        except (APIError, ImageNotFound) as exc:
            raise ContainerExecutionError(f"failed to start workspace container: {exc}") from exc
        if install_dependencies and stack_entry is not None:
            _install_dependencies_if_present(
                container, stack_entry, config.command_timeout_seconds
            )
        yield container
    finally:
        if container is not None:
            try:
                container.remove(force=True)
            except APIError:
                logger.warning(
                    "failed to remove workspace container %s -- it may be orphaned",
                    container.id,
                    exc_info=True,
                )
```

Replace it with:

```python
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
            )
            container.start()
            logger.info(
                "project %s: workspace container %s started", project_id, container.id[:12]
            )
        except (APIError, ImageNotFound) as exc:
            raise ContainerExecutionError(f"failed to start workspace container: {exc}") from exc
        if install_dependencies and stack_entry is not None:
            _install_dependencies_if_present(
                container, stack_entry, config.command_timeout_seconds
            )
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
```

Note: `project_id` is already resolved at the top of the function (line 93-94: `if project_id is None: project_id = project_host_path.name`), so it's in scope here unchanged.

- [ ] **Step 4: Run the test to verify it passes**

Run: `pytest services/executor/tests/unit/test_containers_mocked.py -v`
Expected: PASS, and every pre-existing test in the file still passes (the new logging is additive, doesn't change control flow or return values).

- [ ] **Step 5: Write the failing tests for tool-call logging**

Add to `services/executor/tests/unit/test_tools_mocked.py` (add `import logging` to the imports):

```python
def test_tool_run_command_logs_start_and_exit_code(mock_client, mock_container, config, caplog):
    mock_container.exec_run.return_value = (0, (b"hello\n", b""))

    with caplog.at_level(logging.INFO):
        tool_run_command(mock_client, config, "proj-1", ["echo", "hello"])

    messages = [record.message for record in caplog.records]
    assert any("proj-1" in m and "run_command" in m for m in messages)
    assert any("exit_code=0" in m for m in messages)


def test_tool_write_file_logs_a_warning_on_mkdir_failure(mock_client, mock_container, config, caplog):
    mock_container.exec_run.return_value = (1, (b"", b"permission denied"))

    with caplog.at_level(logging.WARNING):
        with pytest.raises(ContainerExecutionError):
            tool_write_file(mock_client, config, "proj-1", "sub/a.py", "content")

    messages = [record.message for record in caplog.records]
    assert any("proj-1" in m and "write_file" in m and "permission denied" in m for m in messages)
```

- [ ] **Step 6: Run the tests to verify they fail**

Run: `pytest services/executor/tests/unit/test_tools_mocked.py::test_tool_run_command_logs_start_and_exit_code services/executor/tests/unit/test_tools_mocked.py::test_tool_write_file_logs_a_warning_on_mkdir_failure -v`
Expected: FAIL — no matching log records.

- [ ] **Step 7: Add tool-call logging to `tools.py`**

In `services/executor/tools.py`, add a module logger right after the existing imports (after line 29, before `_PYTEST_SUMMARY_RE`):

```python
import logging
```

(add to the top import block, alongside `import io`, `import json`, etc.)

```python
logger = logging.getLogger(__name__)
```

(add this line right after the existing module-level regex constants, e.g. after line 32's `_COVERAGE_TOTAL_RE = ...`)

Then in `tool_run_command` (current body, lines 68-73):

```python
    effective_timeout = timeout if timeout is not None else config.command_timeout_seconds
    project_host_path = _project_host_path(config, project_id)
    with workspace_container(
        client, config, project_host_path, stack=stack, project_id=project_id, install_dependencies=True
    ) as container:
        return exec_in_container(container, command, effective_timeout)
```

Replace with:

```python
    effective_timeout = timeout if timeout is not None else config.command_timeout_seconds
    logger.info("project %s: run_command(%s)", project_id, command)
    project_host_path = _project_host_path(config, project_id)
    with workspace_container(
        client, config, project_host_path, stack=stack, project_id=project_id, install_dependencies=True
    ) as container:
        result = exec_in_container(container, command, effective_timeout)
    logger.info("project %s: run_command exit_code=%d", project_id, result.exit_code)
    return result
```

In `tool_read_file` (current body, lines 99-106):

```python
    container_path = _container_path(path)
    project_host_path = _project_host_path(config, project_id)
    with workspace_container(client, config, project_host_path, stack=stack, project_id=project_id) as container:
        try:
            chunks, _ = container.get_archive(container_path)
        except NotFound as exc:
            raise FileNotFoundError(f"'{path}' not found in project '{project_id}'") from exc
        return _extract_single_file(chunks, os.path.basename(container_path))
```

Replace with:

```python
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
```

In `tool_write_file` (current body, lines 133-149):

```python
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
            raise ContainerExecutionError(
                f"mkdir failed for '{path}': {mkdir_result.stderr}"
            )
        container.put_archive(
            directory, _build_single_file_tar(os.path.basename(container_path), content)
        )
    return True
```

Replace with:

```python
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
        container.put_archive(
            directory, _build_single_file_tar(os.path.basename(container_path), content)
        )
    return True
```

- [ ] **Step 8: Run the tests to verify they pass**

Run: `pytest services/executor/tests/unit/test_tools_mocked.py -v`
Expected: PASS, all tests including pre-existing ones.

- [ ] **Step 9: Write the failing test for the API's error logging**

Add to `services/executor/tests/unit/test_api.py` (add `import logging` to the imports):

```python
def test_run_command_logs_the_exception_before_returning_500(client, monkeypatch, caplog):
    def _raise(*args, **kwargs):
        raise ContainerExecutionError("boom: no such network foundry_workspace")

    monkeypatch.setattr(api_module, "tool_run_command", _raise)

    with caplog.at_level(logging.ERROR):
        response = client.post("/run_command", json={"project_id": "proj-1", "command": ["ls"]})

    assert response.status_code == 500
    assert any(
        "boom: no such network foundry_workspace" in record.message for record in caplog.records
    )
```

- [ ] **Step 10: Run the test to verify it fails**

Run: `pytest services/executor/tests/unit/test_api.py::test_run_command_logs_the_exception_before_returning_500 -v`
Expected: FAIL — no matching log record (the 500 currently carries no log line at all, per the design doc's root-cause finding).

- [ ] **Step 11: Add error logging to `api.py`**

In `services/executor/api.py`, add near the top of the file (after the existing imports, before `class RunCommandRequest`):

```python
import logging
```

(add to the top import block alongside `from fastapi import ...`)

```python
logger = logging.getLogger(__name__)
```

Then, in every one of the seven endpoint functions, add `logger.exception(...)` immediately before each `raise HTTPException(status_code=500, ...)` and each `raise HTTPException(status_code=504, ...)` (the two status codes whose current handling never logs). Do not add logging before the `400`/`404` `ValueError`/`FileNotFoundError` branches — those are caller input errors, not execution failures, and are already self-explanatory from the request that produced them.

`run_command`:

```python
        except CommandTimeoutError as exc:
            logger.exception("project %s: run_command timed out", request.project_id)
            raise HTTPException(status_code=504, detail=str(exc)) from exc
        except ContainerExecutionError as exc:
            logger.exception("project %s: run_command failed", request.project_id)
            raise HTTPException(status_code=500, detail=str(exc)) from exc
```

`read_file`:

```python
        except CommandTimeoutError as exc:
            logger.exception("project %s: read_file timed out", request.project_id)
            raise HTTPException(status_code=504, detail=str(exc)) from exc
        except ContainerExecutionError as exc:
            logger.exception("project %s: read_file failed", request.project_id)
            raise HTTPException(status_code=500, detail=str(exc)) from exc
```

`write_file`:

```python
        except CommandTimeoutError as exc:
            logger.exception("project %s: write_file timed out", request.project_id)
            raise HTTPException(status_code=504, detail=str(exc)) from exc
        except ContainerExecutionError as exc:
            logger.exception("project %s: write_file failed", request.project_id)
            raise HTTPException(status_code=500, detail=str(exc)) from exc
```

`list_files`:

```python
        except ContainerExecutionError as exc:
            logger.exception("project %s: list_files failed", request.project_id)
            raise HTTPException(status_code=500, detail=str(exc)) from exc
```

`run_tests`:

```python
        except CommandTimeoutError as exc:
            logger.exception("project %s: run_tests timed out", request.project_id)
            raise HTTPException(status_code=504, detail=str(exc)) from exc
        except ContainerExecutionError as exc:
            logger.exception("project %s: run_tests failed", request.project_id)
            raise HTTPException(status_code=500, detail=str(exc)) from exc
```

`take_screenshot`:

```python
        except CommandTimeoutError as exc:
            logger.exception("project %s: take_screenshot timed out", request.project_id)
            raise HTTPException(status_code=504, detail=str(exc)) from exc
        except ContainerExecutionError as exc:
            logger.exception("project %s: take_screenshot failed", request.project_id)
            raise HTTPException(status_code=500, detail=str(exc)) from exc
```

`clear_project_cache`:

```python
        except ContainerExecutionError as exc:
            logger.exception("project %s: clear_project_cache failed", request.project_id)
            raise HTTPException(status_code=500, detail=str(exc)) from exc
```

- [ ] **Step 12: Run the tests to verify they pass**

Run: `pytest services/executor/tests/unit/test_api.py -v`
Expected: PASS, all tests including pre-existing ones (`logger.exception` only adds a log record, it never changes the response).

- [ ] **Step 13: Run the whole Executor unit suite**

Run: `pytest services/executor/tests/unit --cov=services/executor --cov-report=term-missing`
Expected: All PASS, coverage unchanged or improved (no new uncovered branches — every new log call is exercised by the tests above or by existing happy-path tests that now also emit INFO logs incidentally).

- [ ] **Step 14: Commit**

```bash
git add services/executor/containers.py services/executor/tools.py services/executor/api.py \
        services/executor/tests/unit/test_containers_mocked.py \
        services/executor/tests/unit/test_tools_mocked.py \
        services/executor/tests/unit/test_api.py
git commit -m "$(cat <<'EOF'
feat(executor): log container lifecycle, tool calls, and 500s

containers.py/tools.py had almost no logging, and api.py's controlled
ContainerExecutionError->HTTPException(500) mapping is deliberate error
handling that FastAPI never prints a traceback for -- the real error text
only ever reached an LLM as a tool-result string, never the Executor's own
container logs. Adds INFO lifecycle logs, WARNING on tool failure, and
logger.exception before every 500/504 conversion.
EOF
)"
```

---

### Task 2: Orchestrator crash visibility (`_invoke_graph` wrapper)

**Files:**
- Modify: `services/orchestrator/service.py`
- Test: `services/orchestrator/tests/unit/test_service.py`

**Interfaces:**
- Consumes: `services.orchestrator.progress_buffer.add_progress(project_id: str, text: str) -> None` (existing, unchanged).
- Produces: nothing other tasks depend on directly, but Task 3/4 push events into the same `progress_buffer` module this task also writes to — no shared state conflict, `add_progress` is designed for concurrent appends from multiple call sites.

- [ ] **Step 1: Write the failing test**

In `services/orchestrator/tests/unit/test_service.py`, add to the imports at the top:

```python
import logging

from services.orchestrator.progress_buffer import get_chunks_since
```

Add this test (place it near `test_resume_known_project_progresses_through_merge_gate_to_done`):

```python
def test_resume_crash_logs_with_project_context_and_records_a_progress_event(tmp_path, caplog):
    """A node raising mid-run (simulated here the same way
    test_activate_resumes_execution_when_stuck_mid_task_with_no_pending_approval
    does -- an under-scripted FakeRouter running out of responses) must
    leave a project-tagged ERROR log line and a "crashed" progress event
    behind, not vanish silently. The exception itself must still propagate
    unchanged, since callers' existing 500-mapping depends on it."""
    db_path = tmp_path / "foundry_memory.db"
    checkpointer = build_sqlite_checkpointer(db_path)
    memory_conn = build_global_memory_connection(db_path)
    router = FakeRouter([READY_TURN, EMPTY_DRAFT, NO_FINDINGS_CRITIQUE, CURATION_OK])
    service = OrchestratorService(
        checkpointer,
        router,
        _StubExecutorClient(),
        memory_conn,
        tmp_path / "projects",
        StacksConfig(stacks={}),
    )
    service.handle_webhook("proj-crash", "build me a thing")

    with caplog.at_level(logging.ERROR):
        with pytest.raises(IndexError):
            service.resume("proj-crash", "approve")

    assert any(
        "proj-crash" in record.message and "graph run crashed" in record.message
        for record in caplog.records
    )
    chunks = [chunk.text for chunk in get_chunks_since("proj-crash")]
    assert any(chunk.startswith("❌ crashed:") for chunk in chunks)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest services/orchestrator/tests/unit/test_service.py::test_resume_crash_logs_with_project_context_and_records_a_progress_event -v`
Expected: FAIL — no log record, no progress chunk (the crash currently propagates with neither).

- [ ] **Step 3: Add the `_invoke_graph` helper and wire it into all five call sites**

In `services/orchestrator/service.py`, add near the top (after the existing imports, before `_TERMINAL_PHASES`):

```python
import logging
```

(add to the existing `import sqlite3` / `import uuid` block)

```python
from services.orchestrator.progress_buffer import add_progress, clear_progress
```

(replace the existing `from services.orchestrator.progress_buffer import clear_progress` line with this one)

```python
logger = logging.getLogger(__name__)
```

(add right after `_ACTIVE_PROJECT_FILENAME = ".active_project_id"`)

Then add this method to `OrchestratorService`, placed right before `handle_webhook`:

```python
    def _invoke_graph(self, input_: Any, project_id: str) -> None:
        """Invokes the compiled graph for project_id, making a crash visible.

        A node raising (e.g. a local model returning malformed tool-call
        JSON deep inside LangChain's client) previously propagated past this
        service with no project-tagged log line anywhere, and no signal to
        anyone watching the project's progress. This logs the crash with a
        full traceback, records a "crashed" progress event any active
        ProgressPoller will pick up on its next GET /stream poll (safe from
        the clear_progress race a terminal-phase event would hit, since a
        crash never flips `phase` into a terminal value), and re-raises
        unchanged -- every caller's existing exception handling (api.py's
        HTTPException mapping, the Gateway's 5xx guard in
        _relay_webhook_result) is untouched.

        Args:
            input_: The value passed to `_graph.invoke` -- an initial
                ProjectState, a `Command(resume=...)`, or None to continue
                from the last checkpoint.
            project_id: The project thread being invoked.
        """
        try:
            self._graph.invoke(input_, config=self._config(project_id))
        except Exception as exc:
            logger.exception("project %s: graph run crashed", project_id)
            add_progress(project_id, f"❌ crashed: {type(exc).__name__}: {exc}")
            raise
```

Now replace each of the five raw `self._graph.invoke(...)` calls:

In `handle_webhook`, line 115:
```python
                self._graph.invoke(initial_state, config=self._config(project_id))
```
becomes:
```python
                self._invoke_graph(initial_state, project_id)
```

In `handle_webhook`, lines 126-128:
```python
                        self._graph.invoke(
                            Command(resume=text), config=self._config(active_id)
                        )
```
becomes:
```python
                        self._invoke_graph(Command(resume=text), active_id)
```

In `handle_webhook`, line 135:
```python
        self._graph.invoke(initial_state, config=self._config(new_id))
```
becomes:
```python
        self._invoke_graph(initial_state, new_id)
```

In `resume`, line 158:
```python
        self._graph.invoke(Command(resume=decision), config=self._config(project_id))
```
becomes:
```python
        self._invoke_graph(Command(resume=decision), project_id)
```

In `activate`, line 189:
```python
            self._graph.invoke(None, config=self._config(project_id))
```
becomes:
```python
            self._invoke_graph(None, project_id)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `pytest services/orchestrator/tests/unit/test_service.py -v`
Expected: All PASS, including `test_activate_resumes_execution_when_stuck_mid_task_with_no_pending_approval` unchanged — its two `pytest.raises(IndexError)` blocks still hold, since `_invoke_graph`'s bare `raise` re-raises the exact original exception.

- [ ] **Step 5: Run the whole Orchestrator unit suite**

Run: `pytest services/orchestrator --cov=services/orchestrator --cov-report=term-missing`
Expected: All PASS.

- [ ] **Step 6: Commit**

```bash
git add services/orchestrator/service.py services/orchestrator/tests/unit/test_service.py
git commit -m "$(cat <<'EOF'
fix(orchestrator): log and surface a crashed graph run

service.py had no logger and no try/except around any of its five
_graph.invoke() call sites, so a node raising (e.g. a local model
returning malformed tool-call JSON) propagated with no project-tagged log
line anywhere. _invoke_graph() now logs the crash with a full traceback
and records a "crashed" progress event for any active ProgressPoller,
then re-raises unchanged -- existing 500-mapping is untouched.
EOF
)"
```

---

### Task 3: Coder tool-call events (replace raw content streaming)

**Files:**
- Modify: `services/orchestrator/agents/coder.py`
- Test: `services/orchestrator/agents/tests/unit/test_coder.py`

**Interfaces:**
- Consumes: `services.orchestrator.progress_buffer.add_progress(project_id: str, text: str) -> None` (existing, unchanged).
- Produces: nothing other tasks depend on.

- [ ] **Step 1: Write the failing tests**

In `services/orchestrator/agents/tests/unit/test_coder.py`, remove this existing test (it asserts the exact raw-content-streaming behavior this task removes):

```python
def test_run_coder_task_streams_partial_content_into_the_progress_buffer():
    router = FakeRouter([AIMessage(content="working on it", tool_calls=[], usage_metadata=usage())])
    executor = RecordingExecutorClient()

    result = asyncio.run(run_coder_task(router, executor, "proj-stream", "trivial task"))

    assert result.success is True
    chunks = get_chunks_since("proj-stream")
    assert "".join(chunk.text for chunk in chunks) == "working on it"
```

Replace it with:

```python
def test_run_coder_task_records_progress_events_for_a_successful_write():
    router = FakeRouter(
        [
            _tool_call_message(
                "write_file", {"path": "models.py", "content": "class User:\n    pass"}, "call_1"
            ),
            _done_message(),
        ]
    )
    executor = RecordingExecutorClient()

    asyncio.run(run_coder_task(router, executor, "proj-progress-write", "Add a User model"))

    chunks = [chunk.text for chunk in get_chunks_since("proj-progress-write")]
    assert "→ write_file(models.py)" in chunks
    assert "✓ wrote models.py" in chunks


def test_run_coder_task_records_a_progress_event_for_a_failed_read():
    router = FakeRouter(
        [
            _tool_call_message("read_file", {"path": "missing.py"}, "call_1"),
            _done_message(),
        ]
    )
    executor = RecordingExecutorClient(files={})

    asyncio.run(run_coder_task(router, executor, "proj-progress-read-fail", "Add a User model"))

    chunks = [chunk.text for chunk in get_chunks_since("proj-progress-read-fail")]
    assert "→ read_file(missing.py)" in chunks
    assert any(chunk.startswith("✗ read_file(missing.py) failed:") for chunk in chunks)


def test_run_coder_task_records_progress_events_for_run_command():
    router = FakeRouter(
        [
            _tool_call_message("run_command", {"command": ["pytest", "-q"]}, "call_1"),
            _done_message(),
        ]
    )
    executor = RecordingExecutorClient()

    asyncio.run(run_coder_task(router, executor, "proj-progress-cmd", "run the tests"))

    chunks = [chunk.text for chunk in get_chunks_since("proj-progress-cmd")]
    assert "→ run_command(pytest -q)" in chunks
    assert "✓ pytest -q (exit 0)" in chunks
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest services/orchestrator/agents/tests/unit/test_coder.py -v`
Expected: The three new tests FAIL (no matching chunks yet); the removed test is gone so nothing to run for it.

- [ ] **Step 3: Remove raw content streaming and add tool-call events**

In `services/orchestrator/agents/coder.py`, add near the top imports (after the existing `from langchain_core.tools import BaseTool, tool` line):

```python
import logging
```

(add this as the first import in the file, before `from langchain_core.messages import ...`)

```python
logger = logging.getLogger(__name__)
```

(add right after `CODER_ROLE = "coder"`)

Replace the whole `_build_tools` function body:

```python
def _build_tools(executor_client: ExecutorClient, project_id: str, stack: str) -> list[BaseTool]:
    """Builds the Coder's three tools, bound to one project's workspace.

    Executor errors are caught and turned into a tool observation rather
    than raised, so the model's own next turn can see what went wrong and
    try a different approach within the same task attempt, before the
    graph node's outer retry budget is ever spent.

    Args:
        executor_client: The Executor client every tool call dispatches through.
        project_id: The project whose workspace these tools operate in.
        stack: The project's declared stack (ADR-028) -- selects the
            Executor's workspace image and cache volume for every call.
    """

    @tool
    async def read_file(path: str) -> str:
        """Reads a file's contents from the project workspace."""
        add_progress(project_id, f"→ read_file({path})")
        logger.info("project %s: read_file(%s)", project_id, path)
        try:
            content = await executor_client.read_file(project_id, path, stack=stack)
        except ExecutorClientError as exc:
            add_progress(project_id, f"✗ read_file({path}) failed: {exc}")
            logger.warning("project %s: read_file(%s) failed: %s", project_id, path, exc)
            return f"error: {exc}"
        add_progress(project_id, f"✓ read {path}")
        return content

    @tool
    async def write_file(path: str, content: str) -> str:
        """Writes content to a file in the project workspace, creating parent directories as needed."""
        add_progress(project_id, f"→ write_file({path})")
        logger.info("project %s: write_file(%s)", project_id, path)
        try:
            await executor_client.write_file(project_id, path, content, stack=stack)
        except ExecutorClientError as exc:
            add_progress(project_id, f"✗ write_file({path}) failed: {exc}")
            logger.warning("project %s: write_file(%s) failed: %s", project_id, path, exc)
            return f"error: {exc}"
        add_progress(project_id, f"✓ wrote {path}")
        return f"wrote {path}"

    @tool
    async def run_command(command: list[str]) -> str:
        """Runs a command inside the project workspace container, given as an argv list, e.g. ["pytest", "-q"]."""
        command_text = " ".join(command)
        add_progress(project_id, f"→ run_command({command_text})")
        logger.info("project %s: run_command(%s)", project_id, command_text)
        try:
            result = await executor_client.run_command(project_id, command, stack=stack)
        except ExecutorClientError as exc:
            add_progress(project_id, f"✗ run_command({command_text}) failed: {exc}")
            logger.warning(
                "project %s: run_command(%s) failed: %s", project_id, command_text, exc
            )
            return f"error: {exc}"
        add_progress(project_id, f"✓ {command_text} (exit {result.exit_code})")
        return f"exit_code={result.exit_code}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"

    return [read_file, write_file, run_command]
```

Then, in `run_coder_task`'s `astream` loop (current lines 226-231):

```python
    for _ in range(max_steps):
        ai_message_chunk: AIMessageChunk | None = None
        async for chunk in llm_with_tools.astream(messages):
            if chunk.content:
                add_progress(project_id, str(chunk.content))
            ai_message_chunk = chunk if ai_message_chunk is None else ai_message_chunk + chunk
        ai_message: AIMessage = ai_message_chunk
```

Replace with:

```python
    for _ in range(max_steps):
        ai_message_chunk: AIMessageChunk | None = None
        async for chunk in llm_with_tools.astream(messages):
            ai_message_chunk = chunk if ai_message_chunk is None else ai_message_chunk + chunk
        ai_message: AIMessage = ai_message_chunk
```

(The `if chunk.content: add_progress(...)` line is deleted outright — that was the unreliable raw-content relay this task replaces. Nothing else in the loop changes: `ai_message_chunk` is still accumulated the same way for tool-dispatch and cost-accounting purposes.)

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest services/orchestrator/agents/tests/unit/test_coder.py -v`
Expected: All PASS, including every pre-existing test in the file (none of them assert on `chunk.content`/raw streaming except the one just removed; `test_run_coder_task_records_a_failed_read_and_still_recovers`'s `"error: 404" in result.transcript[0]` assertion is unaffected since the tool functions' return values are unchanged, only their side effects gained logging/progress calls).

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/agents/coder.py services/orchestrator/agents/tests/unit/test_coder.py
git commit -m "$(cat <<'EOF'
feat(orchestrator): replace raw content streaming with tool-call events

run_coder_task relayed chunk.content into progress_buffer as the model
streamed each turn -- but during tool-calling turns (the overwhelming
majority), models emit little or no free text, so the buffer often stayed
empty for an entire task and the Telegram message never updated. Each of
the three tools now pushes a discrete dispatch/success/failure event
instead, and logs the same at INFO/WARNING.
EOF
)"
```

---

### Task 4: Task-boundary progress events (`node_coder`)

**Files:**
- Modify: `services/orchestrator/graph/nodes.py`
- Test: `services/orchestrator/graph/tests/unit/test_nodes.py`

**Interfaces:**
- Consumes: `services.orchestrator.progress_buffer.add_progress(project_id: str, text: str) -> None` (existing, unchanged).
- Produces: nothing other tasks depend on.

- [ ] **Step 1: Write the failing tests**

In `services/orchestrator/graph/tests/unit/test_nodes.py`, add to the imports at the top (alongside the existing `from services.orchestrator.graph.state import new_project_state` line):

```python
from services.orchestrator.progress_buffer import get_chunks_since
```

Add these tests near the existing `node_coder` tests (e.g. right after `test_node_coder_retries_a_failed_task_below_the_attempt_limit`):

```python
def test_node_coder_records_a_progress_event_when_a_task_succeeds():
    router = FakeRouter([CODER_DONE])
    state = new_project_state("proj-progress-success")
    state["task_queue"] = [
        {"description": "Do the trivial thing", "status": "pending", "attempts": 0}
    ]

    node_coder(state, router, executor_client=_RecordingExecutorClient())

    chunks = [chunk.text for chunk in get_chunks_since("proj-progress-success")]
    assert "✓ task succeeded (0 remaining)" in chunks


def test_node_coder_records_a_progress_event_on_retry():
    router = FakeRouter([_tool_call("read_file", {"path": "missing.py"})] * 10)
    state = new_project_state("proj-progress-retry")
    state["task_queue"] = [
        {"description": "Do the trivial thing", "status": "pending", "attempts": 0}
    ]

    node_coder(state, router, executor_client=_RecordingExecutorClient())

    chunks = [chunk.text for chunk in get_chunks_since("proj-progress-retry")]
    assert "↻ task attempt 1 failed, retrying" in chunks


def test_node_coder_records_a_progress_event_when_blocked_at_the_replan_cap():
    router = FakeRouter([_tool_call("read_file", {"path": "missing.py"})] * 10)
    state = new_project_state("proj-progress-blocked")
    state["replan_count"] = MAX_REPLANS
    state["task_queue"] = [
        {
            "description": "Do the trivial thing",
            "status": "in_progress",
            "attempts": MAX_TASK_ATTEMPTS - 1,
        }
    ]

    node_coder(state, router, executor_client=_RecordingExecutorClient())

    chunks = [chunk.text for chunk in get_chunks_since("proj-progress-blocked")]
    assert f"✗ task blocked after {MAX_TASK_ATTEMPTS} attempts" in chunks
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest services/orchestrator/graph/tests/unit/test_nodes.py -v -k progress_event`
Expected: All three FAIL (no matching chunks yet).

- [ ] **Step 3: Add the progress events to `node_coder`**

In `services/orchestrator/graph/nodes.py`, add to the imports (alongside the existing `from services.orchestrator.memory.global_store import recall_for_task` line):

```python
from services.orchestrator.progress_buffer import add_progress
```

Then, at the task-succeeded branch (current lines 394-397):

```python
        remaining = sum(1 for task in new_queue if task["status"] in ("pending", "in_progress"))
        logger.info(
            "project %s: task succeeded, %d remaining", state["project_id"], remaining
        )
```

becomes:

```python
        remaining = sum(1 for task in new_queue if task["status"] in ("pending", "in_progress"))
        logger.info(
            "project %s: task succeeded, %d remaining", state["project_id"], remaining
        )
        add_progress(state["project_id"], f"✓ task succeeded ({remaining} remaining)")
```

At the "proposing replan" branch (current lines 413-416):

```python
            logger.warning(
                "project %s: task exhausted %d attempts, proposing replan",
                state["project_id"], active_task["attempts"],
            )
```

becomes:

```python
            logger.warning(
                "project %s: task exhausted %d attempts, proposing replan",
                state["project_id"], active_task["attempts"],
            )
            add_progress(
                state["project_id"],
                f"↻ task exhausted {active_task['attempts']} attempts, proposing replan",
            )
```

At the "at replan cap, blocking" branch (current lines 432-435):

```python
        logger.warning(
            "project %s: task exhausted %d attempts at replan cap, blocking",
            state["project_id"], active_task["attempts"],
        )
```

becomes:

```python
        logger.warning(
            "project %s: task exhausted %d attempts at replan cap, blocking",
            state["project_id"], active_task["attempts"],
        )
        add_progress(
            state["project_id"], f"✗ task blocked after {active_task['attempts']} attempts"
        )
```

At the "attempt failed, retrying" branch (current lines 447-451):

```python
    logger.info(
        "project %s: task attempt %d failed, retrying",
        state["project_id"],
        active_task["attempts"],
    )
```

becomes:

```python
    logger.info(
        "project %s: task attempt %d failed, retrying",
        state["project_id"],
        active_task["attempts"],
    )
    add_progress(
        state["project_id"], f"↻ task attempt {active_task['attempts']} failed, retrying"
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest services/orchestrator/graph/tests/unit/test_nodes.py -v`
Expected: All PASS, including every pre-existing test in the file.

- [ ] **Step 5: Run the whole Orchestrator unit suite**

Run: `pytest services/orchestrator --cov=services/orchestrator --cov-report=term-missing`
Expected: All PASS.

- [ ] **Step 6: Commit**

```bash
git add services/orchestrator/graph/nodes.py services/orchestrator/graph/tests/unit/test_nodes.py
git commit -m "$(cat <<'EOF'
feat(orchestrator): push task-boundary progress events from node_coder

Task success, retry, and exhaustion-to-blocked already had server-side
logger.info/warning calls; each now also pushes a matching progress_buffer
event so the Telegram poller's relayed message tracks task-level outcomes,
not just individual tool calls (services/orchestrator/agents/coder.py).
EOF
)"
```

---

### Task 5: ProgressPoller final-state notifications (terminal phase + idle give-up)

**Files:**
- Modify: `services/telegram_gateway/progress_poller.py`
- Test: `services/telegram_gateway/tests/unit/test_progress_poller.py`

**Interfaces:**
- Consumes: nothing from other tasks (this task is self-contained within `services/telegram_gateway/`; it reads `phase` from the `GET /status` response the poller already fetches every cycle, not from `progress_buffer`).
- Produces: nothing other tasks depend on.

- [ ] **Step 1: Write the failing tests**

In `services/telegram_gateway/tests/unit/test_progress_poller.py`, update the import line:

```python
from services.telegram_gateway.progress_poller import TELEGRAM_MESSAGE_LIMIT, ProgressPoller
```

to:

```python
from services.telegram_gateway.progress_poller import (
    _GIVE_UP_MESSAGE,
    TELEGRAM_MESSAGE_LIMIT,
    ProgressPoller,
)
```

Replace the existing `test_poll_once_stops_once_the_project_reaches_a_terminal_phase` test:

```python
def test_poll_once_stops_once_the_project_reaches_a_terminal_phase():
    orchestrator = _fake_orchestrator(
        [_stream_response([])],
        [_status_response("done")],
    )
    bot = _fake_bot()
    poller = ProgressPoller(orchestrator, bot)
    poller._since_seq["proj-1"] = 5
    poller._idle_polls["proj-1"] = 0

    should_continue = asyncio.run(poller._poll_once("proj-1", chat_id=7))

    assert should_continue is False
    assert "proj-1" not in poller._since_seq
```

with:

```python
def test_poll_once_stops_and_sends_a_done_message_once_the_project_reaches_done():
    orchestrator = _fake_orchestrator(
        [_stream_response([])],
        [_status_response("done")],
    )
    bot = _fake_bot()
    poller = ProgressPoller(orchestrator, bot)
    poller._since_seq["proj-1"] = 5
    poller._idle_polls["proj-1"] = 0

    should_continue = asyncio.run(poller._poll_once("proj-1", chat_id=7))

    assert should_continue is False
    assert "proj-1" not in poller._since_seq
    bot.send_message.assert_awaited_once_with(7, "\n✅ project done")


def test_poll_once_stops_and_sends_a_blocked_message_once_the_project_reaches_blocked():
    orchestrator = _fake_orchestrator(
        [_stream_response([])],
        [_status_response("blocked")],
    )
    bot = _fake_bot()
    poller = ProgressPoller(orchestrator, bot)
    poller._since_seq["proj-1"] = 5
    poller._idle_polls["proj-1"] = 0

    should_continue = asyncio.run(poller._poll_once("proj-1", chat_id=7))

    assert should_continue is False
    bot.send_message.assert_awaited_once_with(7, "\n🚫 project blocked")
```

Replace the existing `test_poll_once_stops_after_idle_timeout` test:

```python
def test_poll_once_stops_after_idle_timeout():
    orchestrator = _fake_orchestrator(
        [_stream_response([])],
        [_status_response("implementing")],
    )
    bot = _fake_bot()
    poller = ProgressPoller(orchestrator, bot, max_idle_polls=1)
    poller._since_seq["proj-1"] = -1
    poller._idle_polls["proj-1"] = 0

    should_continue = asyncio.run(poller._poll_once("proj-1", chat_id=7))

    assert should_continue is False
```

with:

```python
def test_poll_once_stops_after_idle_timeout_and_sends_a_give_up_message():
    orchestrator = _fake_orchestrator(
        [_stream_response([])],
        [_status_response("implementing")],
    )
    bot = _fake_bot()
    poller = ProgressPoller(orchestrator, bot, max_idle_polls=1)
    poller._since_seq["proj-1"] = -1
    poller._idle_polls["proj-1"] = 0

    should_continue = asyncio.run(poller._poll_once("proj-1", chat_id=7))

    assert should_continue is False
    bot.send_message.assert_awaited_once_with(7, _GIVE_UP_MESSAGE)
```

Add a new test for `_poll_once_resilient`'s own give-up path (place it after the existing `test_poll_once_resilient_gives_up_after_idle_budget_of_failures`):

```python
def test_poll_once_resilient_sends_a_give_up_message_when_it_exhausts_the_idle_budget():
    orchestrator = MagicMock()
    orchestrator.stream = AsyncMock(side_effect=RuntimeError("connection refused"))
    bot = _fake_bot()
    poller = ProgressPoller(orchestrator, bot, max_idle_polls=2)
    poller._since_seq["proj-1"] = -1
    poller._idle_polls["proj-1"] = 1

    should_continue = asyncio.run(poller._poll_once_resilient("proj-1", chat_id=7))

    assert should_continue is False
    bot.send_message.assert_awaited_once_with(7, _GIVE_UP_MESSAGE)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest services/telegram_gateway/tests/unit/test_progress_poller.py -v`
Expected: `test_poll_once_stops_and_sends_a_done_message_once_the_project_reaches_done`, `test_poll_once_stops_and_sends_a_blocked_message_once_the_project_reaches_blocked`, `test_poll_once_stops_after_idle_timeout_and_sends_a_give_up_message`, and `test_poll_once_resilient_sends_a_give_up_message_when_it_exhausts_the_idle_budget` FAIL — `bot.send_message` isn't called, and `_GIVE_UP_MESSAGE` doesn't exist yet (import error).

- [ ] **Step 3: Add the final-state messages to `progress_poller.py`**

In `services/telegram_gateway/progress_poller.py`, add these constants after the existing `_TRUNCATION_MARKER` (current line 25):

```python
_GIVE_UP_MESSAGE = (
    "\n⚠️ Lost track of this project -- no updates in a while. "
    "Check /status or /continue."
)
```

Add this module-level helper function right after `_truncate_for_telegram` (current lines 28-41), before `class ProgressPoller:`:

```python
def _terminal_message(phase: str) -> str:
    """Builds the final relayed line once a project reaches a terminal phase.

    Args:
        phase: The project's terminal phase, "done" or "blocked".

    Returns:
        A short status line to append to the project's relayed message.
    """
    return "\n✅ project done" if phase == "done" else "\n🚫 project blocked"
```

Replace `_poll_once_resilient` (current lines 92-113):

```python
    async def _poll_once_resilient(self, project_id: str, chat_id: int) -> bool:
        """Runs one poll cycle, surviving transient failures.

        A momentarily-unreachable Orchestrator or a rejected Telegram edit
        must not kill the poll task silently (an asyncio.Task's exception is
        only ever seen at GC time) -- a failed cycle logs, counts as an idle
        poll, and lets the loop try again, so the idle budget still bounds
        how long a persistently-broken project keeps polling.

        Returns:
            True if polling should continue, False once this project's poll
            loop should stop (terminal phase reached, or idle timeout hit).
        """
        try:
            return await self._poll_once(project_id, chat_id)
        except Exception:
            logger.warning("project %s: progress poll failed, retrying", project_id, exc_info=True)
            self._idle_polls[project_id] = self._idle_polls.get(project_id, 0) + 1
            if self._idle_polls[project_id] >= self._max_idle_polls:
                self._stop(project_id)
                return False
            return True
```

with:

```python
    async def _poll_once_resilient(self, project_id: str, chat_id: int) -> bool:
        """Runs one poll cycle, surviving transient failures.

        A momentarily-unreachable Orchestrator or a rejected Telegram edit
        must not kill the poll task silently (an asyncio.Task's exception is
        only ever seen at GC time) -- a failed cycle logs, counts as an idle
        poll, and lets the loop try again, so the idle budget still bounds
        how long a persistently-broken project keeps polling.

        Returns:
            True if polling should continue, False once this project's poll
            loop should stop (terminal phase reached, or idle timeout hit).
        """
        try:
            return await self._poll_once(project_id, chat_id)
        except Exception:
            logger.warning("project %s: progress poll failed, retrying", project_id, exc_info=True)
            self._idle_polls[project_id] = self._idle_polls.get(project_id, 0) + 1
            if self._idle_polls[project_id] >= self._max_idle_polls:
                try:
                    await self._relay(project_id, chat_id, _GIVE_UP_MESSAGE)
                except Exception:
                    logger.warning(
                        "project %s: failed to send give-up message", project_id, exc_info=True
                    )
                self._stop(project_id)
                return False
            return True
```

Replace `_poll_once` (current lines 115-136):

```python
    async def _poll_once(self, project_id: str, chat_id: int) -> bool:
        """Runs one poll cycle for project_id.

        Returns:
            True if polling should continue, False once this project's poll
            loop should stop (terminal phase reached, or idle timeout hit).
        """
        stream_response = await self._orchestrator.stream(project_id, self._since_seq[project_id])
        chunks = stream_response.json().get("chunks", [])
        if chunks:
            self._since_seq[project_id] = max(chunk["seq"] for chunk in chunks)
            self._idle_polls[project_id] = 0
            await self._relay(project_id, chat_id, "".join(chunk["text"] for chunk in chunks))
        else:
            self._idle_polls[project_id] += 1

        status_response = await self._orchestrator.get_status(project_id)
        phase = status_response.json().get("phase")
        if phase in TERMINAL_PHASES or self._idle_polls[project_id] >= self._max_idle_polls:
            self._stop(project_id)
            return False
        return True
```

with:

```python
    async def _poll_once(self, project_id: str, chat_id: int) -> bool:
        """Runs one poll cycle for project_id.

        Returns:
            True if polling should continue, False once this project's poll
            loop should stop (terminal phase reached, or idle timeout hit).
        """
        stream_response = await self._orchestrator.stream(project_id, self._since_seq[project_id])
        chunks = stream_response.json().get("chunks", [])
        if chunks:
            self._since_seq[project_id] = max(chunk["seq"] for chunk in chunks)
            self._idle_polls[project_id] = 0
            await self._relay(project_id, chat_id, "".join(chunk["text"] for chunk in chunks))
        else:
            self._idle_polls[project_id] += 1

        status_response = await self._orchestrator.get_status(project_id)
        phase = status_response.json().get("phase")
        if phase in TERMINAL_PHASES:
            await self._relay(project_id, chat_id, _terminal_message(phase))
            self._stop(project_id)
            return False
        if self._idle_polls[project_id] >= self._max_idle_polls:
            await self._relay(project_id, chat_id, _GIVE_UP_MESSAGE)
            self._stop(project_id)
            return False
        return True
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest services/telegram_gateway/tests/unit/test_progress_poller.py -v`
Expected: All PASS, including every pre-existing test in the file.

- [ ] **Step 5: Run the whole Telegram Gateway unit suite**

Run: `pytest services/telegram_gateway --cov=services/telegram_gateway --cov-report=term-missing`
Expected: All PASS.

- [ ] **Step 6: Commit**

```bash
git add services/telegram_gateway/progress_poller.py services/telegram_gateway/tests/unit/test_progress_poller.py
git commit -m "$(cat <<'EOF'
feat(gateway): send a final message before the poller gives up

ProgressPoller previously stopped silently both when a project reached a
terminal phase and when its idle budget ran out -- either way, the user's
message just stopped updating forever with no signal of what happened.
_poll_once now sends a done/blocked line (read directly from the phase
GET /status already returns, not progress_buffer, since that buffer is
cleared by OrchestratorService.status() the moment it observes a terminal
phase -- before any concurrent poller request could read a chunk from it)
and _poll_once_resilient sends a "lost track" message before giving up on
repeated Orchestrator errors.
EOF
)"
```

---

## Final verification

- [ ] Run the full test suite across all three services and confirm the 80% coverage floor still holds:

Run: `pytest --cov=services --cov-fail-under=80 --cov-report=term-missing`
Expected: All PASS, coverage ≥ 80%.

- [ ] Run lint:

Run: `ruff check . && ruff format --check .`
Expected: Clean.
