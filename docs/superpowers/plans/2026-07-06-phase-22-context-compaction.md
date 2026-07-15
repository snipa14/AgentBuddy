# Context/Compaction Strategy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bound the three unbounded prompt-construction vectors TODO.md's Phase 22 flags — `messages`, `task_queue`, and the Reviewer's full-file-tree reads — so no agent node reconstructs a prompt from ever-growing raw state.

**Architecture:** `ProjectState` gains three plain fields (`messages_summary`, `messages_curated_through`, `changed_files`) using the same "read current value, write the new total" idiom this codebase already uses for `cost_usd_session`/`cost_usd_weekly` — no reducer change. `node_memory_curator` becomes the thing that shrinks the effective `messages` window: on every successful curation it folds newly-seen messages into a rolling summary string and advances a cursor, and both its own prompt and the Planner's `qa_history` read `summary + messages[cursor:]` instead of the full list (or a hardcoded `[-20:]`). The Coder tracks which files it actually wrote (`write_file` tool calls only) and the Reviewer is wired to review only those files instead of the whole repo. `task_queue`'s rendering into the curator's prompt is capped to non-`done` tasks plus the 10 most recent `done` ones.

**Tech Stack:** Python 3.12, LangGraph `TypedDict` state, Pydantic v2, pytest.

## Global Constraints

- No inline comments; self-documenting names (CLAUDE.md).
- Type hints everywhere; no bare `Any` without a comment explaining why (CLAUDE.md).
- Test naming: `test_<thing_under_test>_<condition>_<expected_outcome>` (`.claude/rules/testing.md`).
- **Sandbox gap:** `langgraph`, `langchain_core`, `langchain_anthropic`, `langchain_ollama`, `aiogram`, `pyyaml`, and `ruff` are not installed in this execution sandbox, and host installs are blocked by this repo's own pre-tool-use hooks. Every test file touched by this plan imports at least one of `langchain_core`/`langgraph` transitively, so `pytest` cannot actually execute for real here. For every test-running step below: attempt the `pytest` command exactly as written; if it fails with `ModuleNotFoundError` for one of the packages above, that confirms the known permanent gap (not a real failure) — hand-trace the test body against the implementation instead (read the test, read the code, confirm line-by-line the assertion would hold), and say explicitly in the task's commit message and review notes which verification mode was used (real run vs. hand-trace). Do not attempt `pip install` to fix it.
- No new dependencies are needed anywhere in this plan.
- Work happens directly on `release/v1` — no feature branch or worktree (established project convention).
- Do **not** tag `phase-22-done` or claim "Phase 22 complete" at the end of this plan — Phase 22 is a backlog of 10 independent stretch items in TODO.md and this plan implements exactly one of them (context/compaction strategy). Only check off that single TODO.md bullet.
- Design spec this plan implements: `docs/superpowers/specs/2026-07-06-phase-22-context-compaction-design.md`. Two accepted, out-of-scope limitations documented there and not to be "fixed" as scope creep during this plan: (1) `messages_summary` itself isn't re-summarized if it grows large over an extremely long-lived project; (2) files modified via `run_command` shell redirects instead of the `write_file` tool escape `changed_files` tracking.

---

### Task 1: Document ADR-037 in ARCHITECTURE.md

Per TODO.md's Phase 22 preamble ("Each item should get an `ARCHITECTURE.md` update (new ADR) before implementation starts"), the doc update lands first, describing the target end-state the following tasks implement.

**Files:**
- Modify: `ARCHITECTURE.md` (§10 Decision Log — insert new row after ADR-036)

**Interfaces:**
- Produces: the documented target field names (`messages_summary`, `messages_curated_through`, `changed_files`, `CoderTaskResult.written_files`) — every later task's code must match these exactly.

- [ ] **Step 1: Insert the new Decision Log row**

Read the current last row of the §10 Decision Log table in `ARCHITECTURE.md` first (search for `| 036 |`) to confirm exact current formatting, then insert a new row immediately after it:

```markdown
| 037 | `ProjectState` gains `messages_summary: str`, `messages_curated_through: int`, and `changed_files: list[str]` (plain fields, no reducer -- same "read current value, write the new total" idiom already used for `cost_usd_session`/`cost_usd_weekly`); `node_memory_curator` folds each successful curation into a rolling summary and advances a cursor instead of reading a hardcoded `messages[-20:]` window; `CoderTaskResult` gains `written_files: list[str]` (captured directly off dispatched `write_file` tool calls), which `node_coder` accumulates into `changed_files` across every task/retry and `node_reviewer` passes into `review_changes()`'s existing, previously-unused `changed_files` parameter; `_task_progress_lines()` caps rendered `task_queue` entries to non-`done` tasks plus the 10 most recent `done` ones (Phase 22 stretch) | TODO.md flagged three unbounded prompt-construction vectors: `messages` (only bounded by a hardcoded, silently-lossy `[-20:]` slice in the curator, and read in full by the Planner's interview with no cap at all), `task_queue` (rendered into the curator's prompt in full, forever), and the Reviewer's file-tree read (`review_changes()` already accepted a `changed_files` parameter but `node_reviewer` never passed it, so every review re-read the entire repo). A plain additive summary field was chosen over switching the `messages` reducer to LangGraph's `add_messages`/`RemoveMessage` (which would allow real deletion, not just a bounded read) because that path depends on `BaseMessage` auto-id behavior unverifiable against the pinned `langgraph>=0.2` floor in this sandbox (no real install to check against, and this session had two fixes back-to-back turn out to have hand-trace-only sibling bugs on this exact file) -- the additive approach matches an idiom this codebase already trusts instead of introducing a new one. Design self-review caught that `_extract_description_and_history`'s consumption of the cursor is currently inert in practice: `node_planner_ask` only ever runs before `plan_wait`, and `node_memory_curator` (the only thing that advances the cursor) only ever fires after it -- there is no path back to the Planner post-approval yet (no re-planning phase, a separate, unbuilt Phase 22 stretch item), so the cursor is always `0` at every point that function runs today. Included anyway as forward-proofing, since it shares the same fields the curator fix needs regardless. Two limitations accepted rather than solved: `messages_summary` isn't itself re-summarized if it grows large over an extremely long-lived project, and files modified via `run_command` shell redirects instead of `write_file` escape `changed_files` tracking (the Coder's system prompt steers toward `write_file` but doesn't enforce it) |
```

- [ ] **Step 2: Commit**

```bash
git add ARCHITECTURE.md
git commit -m "$(cat <<'EOF'
docs: add ADR-037 for context/compaction strategy (Phase 22 stretch)
EOF
)"
```

---

### Task 2: Add compaction fields to ProjectState

**Files:**
- Modify: `services/orchestrator/graph/state.py`
- Test: `services/orchestrator/graph/tests/unit/test_state.py`

**Interfaces:**
- Produces: `ProjectState.messages_summary: str`, `ProjectState.messages_curated_through: int`, `ProjectState.changed_files: list[str]`, all defaulted in `new_project_state()` — every later task reads/writes these exact field names.

- [ ] **Step 1: Write the failing tests**

Append to `services/orchestrator/graph/tests/unit/test_state.py`:

```python
def test_new_project_state_defaults_compaction_fields_to_empty():
    state = new_project_state("proj-1")

    assert state["messages_summary"] == ""
    assert state["messages_curated_through"] == 0
    assert state["changed_files"] == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest services/orchestrator/graph/tests/unit/test_state.py::test_new_project_state_defaults_compaction_fields_to_empty -v`
Expected: FAIL with `KeyError: 'messages_summary'` (or a `ModuleNotFoundError` per Global Constraints — in that case hand-trace: confirm `state.py` doesn't yet define these keys, so the assertion would raise `KeyError`).

- [ ] **Step 3: Add the fields to `ProjectState` and `new_project_state()`**

In `services/orchestrator/graph/state.py`, in the `ProjectState` class body, add after the existing `curator_source: str | None` line:

```python
    messages_summary: str
    messages_curated_through: int
    changed_files: list[str]
```

In `new_project_state()`'s `return ProjectState(...)` call, add after the existing `curator_source=None,` line:

```python
        messages_summary="",
        messages_curated_through=0,
        changed_files=[],
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest services/orchestrator/graph/tests/unit/test_state.py -v`
Expected: PASS for all tests in the file (or hand-trace per Global Constraints: re-read the modified `new_project_state()` return statement and confirm each new key's default matches the assertion).

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/graph/state.py services/orchestrator/graph/tests/unit/test_state.py
git commit -m "$(cat <<'EOF'
feat(orchestrator): add compaction fields to ProjectState (ADR-037)
EOF
)"
```

---

### Task 3: Curator-driven messages compaction

**Files:**
- Modify: `services/orchestrator/graph/nodes.py` (`node_memory_curator`)
- Test: `services/orchestrator/graph/tests/unit/test_memory_curator.py`

**Interfaces:**
- Consumes: `ProjectState.messages_summary`/`messages_curated_through` (Task 2).
- Produces: `node_memory_curator`'s returned update dict includes `messages_summary`/`messages_curated_through` on every successful curation (never on the existing `except Exception` no-op path) — Task 4 depends on these being correctly populated in state going forward.

- [ ] **Step 1: Write the failing tests**

Add to `services/orchestrator/graph/tests/unit/test_memory_curator.py`. First, extend the import line `from unittest.mock import MagicMock` to also import `AsyncMock` and `patch`:

```python
from unittest.mock import AsyncMock, MagicMock, patch
```

Add `from langchain_core.messages import AIMessage` to the imports.

Then modify the existing `test_node_memory_curator_runs_curation_and_stores` test (add two assertions at the end, right before `conn.close()`):

```python
def test_node_memory_curator_runs_curation_and_stores(tmp_path):
    """Curation runs, writes LEARNINGS.md, records global memory, and folds
    the completion's cost into cost_usd_session/cost_usd_weekly (ADR-026)."""
    router = _curator_router()
    from services.orchestrator.memory.global_store import build_global_memory_connection

    conn = build_global_memory_connection(tmp_path / "foundry_memory.db")
    projects_root = tmp_path / "projects"
    projects_root.mkdir()

    state = new_project_state("proj-curate", initial_message="build a thing")
    state["curator_source"] = "testing"
    state["messages"] = [MagicMock(content="did implementation"), MagicMock(content="ran tests")]

    update = node_memory_curator(state, projects_root, conn, router)

    assert update["cost_usd_session"] > state["cost_usd_session"]
    assert update["cost_usd_weekly"] > state["cost_usd_weekly"]
    assert update["messages_summary"] == "Completed implementation and testing for proj-cross-project.\n"
    assert update["messages_curated_through"] == 2
    # Check LEARNINGS.md was written
    learnings = (projects_root / "proj-curate" / "LEARNINGS.md").read_text()
    assert "Completed implementation and testing" in learnings
    # Check global memory was recorded
    from services.orchestrator.memory.global_store import recall

    results = recall(conn, "flaky")
    assert len(results) == 1
    assert results[0].type == "error_pattern"
    conn.close()
```

Add two new tests, placed after `test_node_memory_curator_runs_curation_and_stores`:

```python
def test_node_memory_curator_appends_to_existing_summary_rather_than_overwriting(tmp_path):
    """A second curation checkpoint's summary is appended to the first's, not
    replacing it -- messages_summary is the compacted stand-in for
    everything curated so far, not just the most recent phase."""
    router = _curator_router()
    from services.orchestrator.memory.global_store import build_global_memory_connection

    conn = build_global_memory_connection(tmp_path / "foundry_memory.db")
    projects_root = tmp_path / "projects"
    projects_root.mkdir()

    state = new_project_state("proj-curate", initial_message="build a thing")
    state["curator_source"] = "testing"
    state["messages"] = [MagicMock(content="did implementation"), MagicMock(content="ran tests")]
    state["messages_summary"] = "Prior phase: planned the project."
    state["messages_curated_through"] = 0

    update = node_memory_curator(state, projects_root, conn, router)

    assert update["messages_summary"] == (
        "Prior phase: planned the project.\n\n"
        "Completed implementation and testing for proj-cross-project.\n"
    )
    assert update["messages_curated_through"] == 2
    conn.close()


def test_node_memory_curator_includes_all_events_since_cursor_not_just_last_20(tmp_path):
    """Regression test for the removed hardcoded messages[-20:] window: with
    more than 20 events accumulated since the last curation checkpoint, every
    one of them must reach the curator, not just the most recent 20."""
    router = _curator_router()
    projects_root = tmp_path / "projects"
    projects_root.mkdir()
    conn = sqlite3.connect(":memory:")

    state = new_project_state("proj-curate")
    state["curator_source"] = "testing"
    state["messages"] = [MagicMock(content=f"event {i}") for i in range(25)]
    state["messages_curated_through"] = 0

    fake_ai_message = AIMessage(
        content="", usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}
    )
    with patch(
        "services.orchestrator.graph.nodes.curate_and_store_phase",
        new=AsyncMock(return_value=(_CURATION_RESULT, fake_ai_message)),
    ) as mock_curate:
        node_memory_curator(state, projects_root, conn, router)

    phase_events = mock_curate.call_args.args[4]
    assert len(phase_events) == 25
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest services/orchestrator/graph/tests/unit/test_memory_curator.py -v`
Expected: the two new tests FAIL — `test_node_memory_curator_appends_to_existing_summary_rather_than_overwriting` and the modified `test_node_memory_curator_runs_curation_and_stores` with `KeyError: 'messages_summary'`; `test_node_memory_curator_includes_all_events_since_cursor_not_just_last_20` FAILs with `assert 20 == 25` (the current hardcoded `[-20:]` slice). (Or hand-trace per Global Constraints: confirm `node_memory_curator`'s current body — read `services/orchestrator/graph/nodes.py:703-733` — returns `{}` plus cost keys only, never `messages_summary`, and slices `state["messages"][-20:]` unconditionally.)

- [ ] **Step 3: Rewrite `node_memory_curator`**

In `services/orchestrator/graph/nodes.py`, replace the whole function body (lines 703-733, i.e. everything from `source = state.get("curator_source")` through the final `except` block) with:

```python
    source = state.get("curator_source")
    if not source:
        return {}

    project_id = state["project_id"]
    cursor = state["messages_curated_through"]
    phase_events = [msg.content for msg in state["messages"][cursor:]]
    if phase_events:
        phase_events = phase_events + _task_progress_lines(state["task_queue"])
    if not phase_events:
        logger.info("project %s: nothing to curate (no events)", project_id)
        return {}

    created_at = datetime.datetime.now(datetime.timezone.utc).isoformat()

    try:
        result, ai_message = asyncio.run(
            curate_and_store_phase(
                router,
                projects_root,
                conn,
                project_id,
                phase_events,
                created_at=created_at,
            )
        )
        cost = router.cost_usd(CURATOR_ROLE, ai_message.usage_metadata)
        logger.info("project %s: memory curated from %d event(s)", project_id, len(phase_events))
        new_summary = (
            f"{state['messages_summary']}\n\n{result.summary_markdown}"
            if state["messages_summary"]
            else result.summary_markdown
        )
        return _with_cost(
            state,
            {
                "messages_summary": new_summary,
                "messages_curated_through": len(state["messages"]),
            },
            cost,
        )
    except Exception:
        logger.warning("project %s: curator failed — continuing without curation", project_id)
        return {}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest services/orchestrator/graph/tests/unit/test_memory_curator.py -v`
Expected: PASS for every test in the file, including the pre-existing `test_node_memory_curator_continues_on_failure` (unmodified — its `assert update == {}` already proves the `except` path returns no compaction fields) and `test_node_memory_curator_skips_when_no_events` (unmodified — empty `messages` still yields empty `phase_events` regardless of cursor value). (Or hand-trace: re-read the new function body against each test's setup and assertions line by line.)

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/graph/nodes.py services/orchestrator/graph/tests/unit/test_memory_curator.py
git commit -m "$(cat <<'EOF'
feat(orchestrator): curator-driven messages compaction (ADR-037)

node_memory_curator now folds each successful curation into a rolling
messages_summary and advances messages_curated_through, reading
messages[cursor:] instead of a hardcoded, silently-lossy messages[-20:]
window.
EOF
)"
```

---

### Task 4: Planner reads the compacted summary instead of full history

**Files:**
- Modify: `services/orchestrator/graph/nodes.py` (`_extract_description_and_history`)
- Test: `services/orchestrator/graph/tests/unit/test_nodes.py`

**Interfaces:**
- Consumes: `ProjectState.messages_summary`/`messages_curated_through` (Task 2), populated by `node_memory_curator` (Task 3).
- Produces: no change to `_extract_description_and_history`'s signature or callers (`node_planner_ask`, `node_planner_draft`, `node_planner_critique` are unaffected at their call sites).

- [ ] **Step 1: Write the failing tests**

Add to `services/orchestrator/graph/tests/unit/test_nodes.py`, near the other `node_planner_ask` tests (after `test_node_planner_ask_advances_to_roasting_when_ready`):

```python
def test_extract_description_and_history_uses_summary_and_cursor_when_set():
    from services.orchestrator.graph.nodes import _extract_description_and_history
    from langchain_core.messages import HumanMessage

    state = new_project_state("proj-1")
    state["messages"] = [
        HumanMessage(content="original request"),
        HumanMessage(content="old question"),
        HumanMessage(content="old answer"),
        HumanMessage(content="new question"),
        HumanMessage(content="new answer"),
    ]
    state["messages_summary"] = "Stakeholder already answered the basics."
    state["messages_curated_through"] = 3

    description, qa_history = _extract_description_and_history(state)

    assert description == "original request"
    assert qa_history == [
        "Stakeholder already answered the basics.",
        "new question",
        "new answer",
    ]


def test_extract_description_and_history_omits_summary_when_never_curated():
    from services.orchestrator.graph.nodes import _extract_description_and_history

    state = new_project_state("proj-1", initial_message="original request")

    description, qa_history = _extract_description_and_history(state)

    assert description == "original request"
    assert qa_history == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest services/orchestrator/graph/tests/unit/test_nodes.py::test_extract_description_and_history_uses_summary_and_cursor_when_set -v`
Expected: FAIL — the current implementation ignores `messages_summary`/`messages_curated_through` entirely and returns `qa_history == ["old question", "old answer", "new question", "new answer"]` (all four, no summary line). (Or hand-trace: read the current `_extract_description_and_history` at `services/orchestrator/graph/nodes.py:128-135` and confirm it reads `messages[1:]` unconditionally.)

- [ ] **Step 3: Rewrite `_extract_description_and_history`**

In `services/orchestrator/graph/nodes.py`, replace the function body (lines 128-135):

```python
def _extract_description_and_history(state: ProjectState) -> tuple[str, list[str]]:
    """Splits `messages` into the original project request and the interview transcript."""
    messages = state["messages"]
    if not messages:
        return "", []
    description = messages[0].content
    cursor = max(1, state["messages_curated_through"])
    tail = [message.content for message in messages[cursor:]]
    summary = state["messages_summary"]
    qa_history = ([summary] if summary else []) + tail
    return description, qa_history
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest services/orchestrator/graph/tests/unit/test_nodes.py -v`
Expected: PASS for the two new tests and every pre-existing test in the file — in particular `test_node_planner_ask_arms_the_question_gate_when_more_info_needed` and `test_node_planner_ask_advances_to_roasting_when_ready`, both of which use `new_project_state("proj-1", initial_message="build a thing")` with no curation ever having run, so `messages_curated_through` stays at its default `0`, `cursor = max(1, 0) = 1`, and `qa_history` reads `messages[1:]` exactly as before. (Or hand-trace: confirm this equivalence for both tests by re-reading `new_project_state`'s defaults.)

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/graph/nodes.py services/orchestrator/graph/tests/unit/test_nodes.py
git commit -m "$(cat <<'EOF'
feat(orchestrator): Planner interview reads compacted summary + tail (ADR-037)
EOF
)"
```

---

### Task 5: Cap task_queue's rendering into the curator prompt

**Files:**
- Modify: `services/orchestrator/graph/nodes.py` (`_task_progress_lines`)
- Test: `services/orchestrator/graph/tests/unit/test_nodes.py`

**Interfaces:**
- Produces: `_task_progress_lines(task_queue, recent_done_limit=10)` — the new parameter has a default, so `node_memory_curator`'s existing call site (`_task_progress_lines(state["task_queue"])`, unchanged) keeps working without modification.

- [ ] **Step 1: Write the failing test**

Add to `services/orchestrator/graph/tests/unit/test_nodes.py`, right after `test_task_progress_lines_returns_empty_list_for_empty_queue`:

```python
def test_task_progress_lines_caps_done_tasks_to_the_most_recent_limit():
    from services.orchestrator.graph.nodes import _task_progress_lines

    task_queue = (
        [{"description": f"Old done task {i}", "status": "done", "attempts": 0} for i in range(15)]
        + [{"description": "Still pending", "status": "pending", "attempts": 0}]
    )

    lines = _task_progress_lines(task_queue, recent_done_limit=10)

    assert len(lines) == 11
    assert lines[:10] == [
        f"[done] Old done task {i} (attempts: 0)" for i in range(5, 15)
    ]
    assert lines[10] == "[pending] Still pending (attempts: 0)"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest services/orchestrator/graph/tests/unit/test_nodes.py::test_task_progress_lines_caps_done_tasks_to_the_most_recent_limit -v`
Expected: FAIL — `TypeError: _task_progress_lines() got an unexpected keyword argument 'recent_done_limit'`. (Or hand-trace: confirm the current signature is `_task_progress_lines(task_queue: list[dict]) -> list[str]` with no second parameter.)

- [ ] **Step 3: Rewrite `_task_progress_lines`**

In `services/orchestrator/graph/nodes.py`, replace the function (currently at lines 113-119):

```python
def _task_progress_lines(task_queue: list[dict], recent_done_limit: int = 10) -> list[str]:
    """Renders each task_queue item as a one-line status summary for the
    Memory Curator's input, e.g. "[done] Add a User model (attempts: 1)".
    Every non-done task is kept (always actionable/interesting); done tasks
    are capped to the most recent `recent_done_limit` so a long-lived
    project's curator prompt doesn't grow with total completed-task count
    forever."""
    done_indices = [i for i, task in enumerate(task_queue) if task["status"] == "done"]
    keep_done = set(done_indices[-recent_done_limit:])
    kept = [
        task for i, task in enumerate(task_queue) if task["status"] != "done" or i in keep_done
    ]
    return [
        f"[{task['status']}] {task['description']} (attempts: {task['attempts']})"
        for task in kept
    ]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest services/orchestrator/graph/tests/unit/test_nodes.py -v`
Expected: PASS for the new test and the two pre-existing `_task_progress_lines` tests (`test_task_progress_lines_renders_each_task_with_status_and_attempts`, `test_task_progress_lines_returns_empty_list_for_empty_queue` — neither has more than 10 done tasks, so the default cap changes nothing for them). (Or hand-trace each against the new implementation.)

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/graph/nodes.py services/orchestrator/graph/tests/unit/test_nodes.py
git commit -m "$(cat <<'EOF'
feat(orchestrator): cap task_queue rendering in curator prompt (ADR-037)
EOF
)"
```

---

### Task 6: Coder tracks files it actually writes

**Files:**
- Modify: `services/orchestrator/agents/coder.py` (`CoderTaskResult`, `run_coder_task`)
- Test: `services/orchestrator/agents/tests/unit/test_coder.py`

**Interfaces:**
- Produces: `CoderTaskResult.written_files: list[str]` — Task 7 (`node_coder`) reads this field from every `run_coder_task` result.

- [ ] **Step 1: Write the failing test**

Add to `services/orchestrator/agents/tests/unit/test_coder.py`, right after `test_run_coder_task_reads_then_writes_then_finishes`:

```python
def test_run_coder_task_records_written_file_paths():
    router = FakeRouter(
        [
            _tool_call_message("read_file", {"path": "models.py"}, "call_1"),
            _tool_call_message(
                "write_file", {"path": "models.py", "content": "class User:\n    pass"}, "call_2"
            ),
            _tool_call_message(
                "write_file", {"path": "views.py", "content": "def login(): ..."}, "call_3"
            ),
            _done_message(),
        ]
    )
    executor = RecordingExecutorClient(files={"models.py": "# empty\n"})

    result = asyncio.run(run_coder_task(router, executor, "proj-1", "Add a User model"))

    assert result.written_files == ["models.py", "views.py"]


def test_run_coder_task_written_files_empty_when_no_writes_happen():
    router = FakeRouter([_done_message()])
    executor = RecordingExecutorClient()

    result = asyncio.run(run_coder_task(router, executor, "proj-1", "trivial task"))

    assert result.written_files == []


def test_run_coder_task_records_written_files_even_when_max_steps_exhausted():
    router = FakeRouter(
        [
            _tool_call_message(
                "write_file", {"path": "partial.py", "content": "# wip"}, "call_1"
            )
        ]
        * 3
    )
    executor = RecordingExecutorClient()

    result = asyncio.run(
        run_coder_task(router, executor, "proj-1", "Add a User model", max_steps=3)
    )

    assert result.success is False
    assert result.written_files == ["partial.py", "partial.py", "partial.py"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest services/orchestrator/agents/tests/unit/test_coder.py -v`
Expected: the three new tests FAIL with `AttributeError: 'CoderTaskResult' object has no attribute 'written_files'`. (Or hand-trace: confirm `CoderTaskResult` currently has only `success`, `transcript`, `cost_usd`.)

- [ ] **Step 3: Add `written_files` to `CoderTaskResult` and populate it in `run_coder_task`**

In `services/orchestrator/agents/coder.py`, change the `CoderTaskResult` class:

```python
class CoderTaskResult(BaseModel):
    """The outcome of running a single task to completion or exhausting max_steps."""

    success: bool
    transcript: list[str]
    written_files: list[str] = []
    cost_usd: float = 0.0
```

In `run_coder_task`, add a `written_files: list[str] = []` alongside the existing `transcript: list[str] = []` declaration:

```python
    transcript: list[str] = []
    written_files: list[str] = []
    total_cost = 0.0
```

In the `for call in ai_message.tool_calls:` loop, after the existing `transcript.append(...)` line, add:

```python
            if call["name"] == "write_file":
                written_files.append(call["args"]["path"])
```

Update both `return CoderTaskResult(...)` statements (the success path inside the loop, and the max-steps-exhausted path at the end of the function) to include `written_files=written_files`:

```python
            return CoderTaskResult(
                success=True, transcript=transcript, written_files=written_files, cost_usd=total_cost
            )
```

```python
    return CoderTaskResult(
        success=False, transcript=transcript, written_files=written_files, cost_usd=total_cost
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest services/orchestrator/agents/tests/unit/test_coder.py -v`
Expected: PASS for all tests in the file, including every pre-existing test (none of them assert on `written_files`, and the field defaults to `[]` so no existing equality assertion on a full `CoderTaskResult` object breaks — confirm none of the existing tests construct a `CoderTaskResult` directly for comparison; they only read `.success`/`.transcript`/`.cost_usd`). (Or hand-trace each new test against the updated function body.)

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/agents/coder.py services/orchestrator/agents/tests/unit/test_coder.py
git commit -m "$(cat <<'EOF'
feat(orchestrator): Coder tracks write_file-touched paths (ADR-037)
EOF
)"
```

---

### Task 7: node_coder accumulates changed_files across tasks and retries

**Files:**
- Modify: `services/orchestrator/graph/nodes.py` (`node_coder`)
- Test: `services/orchestrator/graph/tests/unit/test_nodes.py`

**Interfaces:**
- Consumes: `CoderTaskResult.written_files` (Task 6), `ProjectState.changed_files` (Task 2).
- Produces: `node_coder`'s returned update dict includes `changed_files` on every path (success, retry, blocked) — Task 8 (`node_reviewer`) reads `state["changed_files"]`.

- [ ] **Step 1: Write the failing tests**

Add to `services/orchestrator/graph/tests/unit/test_nodes.py`, right after `test_node_coder_keeps_implementing_when_more_tasks_remain`. First, a helper to script a coder run that writes a file — add near the top of the file, right after `_tool_call`:

```python
def _write_then_done(path: str) -> list:
    return [_tool_call("write_file", {"path": path, "content": "x"}), CODER_DONE]
```

Then the new tests:

```python
def test_node_coder_accumulates_changed_files_across_tasks():
    router = FakeRouter(_write_then_done("models.py") + _write_then_done("views.py"))
    state = new_project_state("proj-1")
    state["task_queue"] = [
        {"description": "First task", "status": "pending", "attempts": 0},
        {"description": "Second task", "status": "pending", "attempts": 0},
    ]

    first_update = node_coder(state, router, executor_client=_RecordingExecutorClient())
    state.update(first_update)
    second_update = node_coder(state, router, executor_client=_RecordingExecutorClient())

    assert second_update["changed_files"] == ["models.py", "views.py"]


def test_node_coder_dedupes_changed_files_across_retries():
    router = FakeRouter(_write_then_done("models.py"))
    state = new_project_state("proj-1")
    state["changed_files"] = ["models.py"]
    state["task_queue"] = [
        {"description": "Do the trivial thing", "status": "pending", "attempts": 0}
    ]

    update = node_coder(state, router, executor_client=_RecordingExecutorClient())

    assert update["changed_files"] == ["models.py"]


def test_node_coder_records_changed_files_even_when_task_blocks():
    router = FakeRouter(_write_then_done("partial.py")[:1] * 10)
    state = new_project_state("proj-1")
    state["task_queue"] = [
        {
            "description": "Do the trivial thing",
            "status": "in_progress",
            "attempts": MAX_TASK_ATTEMPTS - 1,
        }
    ]

    update = node_coder(state, router, executor_client=_RecordingExecutorClient())

    assert update["phase"] == "blocked"
    assert update["changed_files"] == ["partial.py"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest services/orchestrator/graph/tests/unit/test_nodes.py::test_node_coder_accumulates_changed_files_across_tasks -v`
Expected: FAIL — `KeyError: 'changed_files'` (the current `node_coder` never returns this key). (Or hand-trace: read the current `node_coder` at `services/orchestrator/graph/nodes.py:265-339` and confirm none of its three `return`/`_with_cost` calls include `changed_files`.)

- [ ] **Step 3: Update `node_coder` to accumulate `changed_files`**

In `services/orchestrator/graph/nodes.py`, in `node_coder`, add this line immediately after `result = asyncio.run(run_coder_task(...))` (before the `if result.success:` branch):

```python
    new_changed_files = list(dict.fromkeys(state["changed_files"] + result.written_files))
```

Then add `"changed_files": new_changed_files,` to each of the three dicts already being returned/passed to `_with_cost`:

The success branch (currently `{"task_queue": new_queue, "phase": "implementing" if remaining else "reviewing"}`) becomes:

```python
        return _with_cost(
            state,
            {
                "task_queue": new_queue,
                "phase": "implementing" if remaining else "reviewing",
                "changed_files": new_changed_files,
            },
            result.cost_usd,
        )
```

The blocked branch (currently `{"task_queue": new_queue, "phase": "blocked", "curator_source": "coder"}`) becomes:

```python
        return _with_cost(
            state,
            {
                "task_queue": new_queue,
                "phase": "blocked",
                "curator_source": "coder",
                "changed_files": new_changed_files,
            },
            result.cost_usd,
        )
```

The retry branch (currently `{"task_queue": new_queue, "phase": "implementing"}`) becomes:

```python
    return _with_cost(
        state,
        {"task_queue": new_queue, "phase": "implementing", "changed_files": new_changed_files},
        result.cost_usd,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest services/orchestrator/graph/tests/unit/test_nodes.py -v`
Expected: PASS for the three new tests and every pre-existing `node_coder` test (`test_node_coder_returns_reviewing_phase_when_queue_already_empty`, `test_node_coder_returns_reviewing_phase_when_every_task_is_already_done`, `test_node_coder_succeeds_and_advances_to_reviewing_when_last_task_done`, `test_node_coder_keeps_implementing_when_more_tasks_remain`, `test_node_coder_resumes_the_in_progress_task_rather_than_the_first_pending_one`, `test_node_coder_retries_a_failed_task_below_the_attempt_limit`, `test_node_coder_blocks_after_exhausting_the_attempt_budget` — none of these assert `update == {...}` as a full-dict equality without `changed_files`, they all assert specific keys like `update["task_queue"]`/`update["phase"]`, so the new key doesn't break them). Note the two early-return tests (`..._queue_already_empty`, `..._every_task_is_already_done`) return `{"phase": "reviewing"}` directly, before `result`/`new_changed_files` are ever computed — those two paths are unaffected by this change and correctly stay as bare `{"phase": "reviewing"}`. (Or hand-trace each against the updated function body.)

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/graph/nodes.py services/orchestrator/graph/tests/unit/test_nodes.py
git commit -m "$(cat <<'EOF'
feat(orchestrator): node_coder accumulates changed_files (ADR-037)
EOF
)"
```

---

### Task 8: node_reviewer scopes review to changed_files

**Files:**
- Modify: `services/orchestrator/graph/nodes.py` (`node_reviewer`)
- Test: `services/orchestrator/graph/tests/unit/test_nodes.py`

**Interfaces:**
- Consumes: `ProjectState.changed_files` (Task 2, populated by Task 7).

- [ ] **Step 1: Write the failing test**

Add to `services/orchestrator/graph/tests/unit/test_nodes.py`, right after `test_node_reviewer_approves_and_advances_to_testing`:

```python
def test_node_reviewer_reads_only_changed_files_when_populated():
    router = FakeRouter([_APPROVE_REVIEW])
    state = new_project_state("proj-1")
    state["changed_files"] = ["models.py"]
    executor = _RecordingExecutorClient(files={"models.py": "class User: pass", "unrelated.py": "x = 1"})

    node_reviewer(state, router, executor)

    assert executor.calls == [("read_file", "proj-1", "models.py")]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest services/orchestrator/graph/tests/unit/test_nodes.py::test_node_reviewer_reads_only_changed_files_when_populated -v`
Expected: FAIL — `executor.calls` instead shows a `list_files` call followed by `read_file` calls for both `models.py` and `unrelated.py` (the current `node_reviewer` calls `review_changes` with no `changed_files` argument, so it lists and reads everything). (Or hand-trace: read the current `node_reviewer` at `services/orchestrator/graph/nodes.py:352-360` and confirm the `review_changes(router, executor_client, state["project_id"])` call passes no fourth argument.)

- [ ] **Step 3: Pass `changed_files` through in `node_reviewer`**

In `services/orchestrator/graph/nodes.py`, in `node_reviewer`, change:

```python
    result, ai_message = asyncio.run(review_changes(router, executor_client, state["project_id"]))
```

to:

```python
    result, ai_message = asyncio.run(
        review_changes(router, executor_client, state["project_id"], changed_files=state["changed_files"])
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest services/orchestrator/graph/tests/unit/test_nodes.py -v`
Expected: PASS for the new test and both pre-existing `node_reviewer` tests (`test_node_reviewer_approves_and_advances_to_testing`, `test_node_reviewer_requests_changes_and_routes_back_to_coder` — both use `new_project_state("proj-1")` with `changed_files` defaulting to `[]`, which is falsy, so `review_changes` falls back to its existing "list everything" branch exactly as before — no behavior change for these two). (Or hand-trace: confirm `review_changes`'s `if changed_files:` branch treats an empty list the same as `None`.)

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/graph/nodes.py services/orchestrator/graph/tests/unit/test_nodes.py
git commit -m "$(cat <<'EOF'
feat(orchestrator): node_reviewer scopes review to changed_files (ADR-037)
EOF
)"
```

---

### Task 9: Check off the TODO.md item and final verification pass

**Files:**
- Modify: `TODO.md`

**Interfaces:**
- Consumes: nothing new — this task only verifies the prior 8 tasks' combined result and updates the backlog checkbox.

- [ ] **Step 1: Check off the TODO.md bullet**

In `TODO.md`, change the "Context management / compaction strategy" bullet from:

```markdown
- [ ] Context management / compaction strategy: no phase addresses what happens when `messages`, `task_queue`, or a project's own file tree grow large enough to approach a provider's context window on a long-running or brownfield project -- every agent node currently reconstructs its prompt from full, uncompacted state on every call. Needs a documented summarization/compaction approach (even a simple one, e.g. curator-driven pruning of `messages`) before Foundry can be trusted on large or long-lived codebases
```

to:

```markdown
- [x] Context management / compaction strategy: `ProjectState` gains `messages_summary`/`messages_curated_through` (a rolling curator-maintained summary + cursor, replacing a hardcoded `messages[-20:]` window) and `changed_files` (Coder-tracked `write_file` paths, wired into the Reviewer's existing but previously-unused `changed_files` scoping instead of always reading the whole repo); `task_queue`'s curator rendering is capped to non-done tasks plus the 10 most recent done ones (ADR-037)
```

Do **not** check off any other Phase 22 bullet, and do not tag `phase-22-done` — the other 9 stretch items in Phase 22 remain unimplemented.

- [ ] **Step 2: Run every test file touched across this whole plan, one more time, together**

Run:

```bash
pytest \
  services/orchestrator/graph/tests/unit/test_state.py \
  services/orchestrator/graph/tests/unit/test_nodes.py \
  services/orchestrator/graph/tests/unit/test_memory_curator.py \
  services/orchestrator/graph/tests/unit/test_concurrency.py \
  services/orchestrator/graph/tests/unit/test_graph_end_to_end.py \
  services/orchestrator/agents/tests/unit/test_coder.py \
  services/orchestrator/agents/tests/unit/test_reviewer.py \
  services/orchestrator/tests/unit/test_service.py \
  services/orchestrator/tests/unit/test_api.py \
  -v
```

Expected: `PASS` for every test (or, per Global Constraints, `ModuleNotFoundError` — in that case this is the final hand-trace pass: re-read every diff from Tasks 1-8 side by side with each test file's current content one more time, confirming every state dict constructed anywhere in `test_concurrency.py`, `test_graph_end_to_end.py`, `test_service.py`, and `test_api.py` still goes through `new_project_state()` — not a hand-built dict literal that would be missing the three new required `ProjectState` keys — and that `grep -rn "changed_files\|messages_summary\|messages_curated_through" services/ --include=*.py` shows every production read matching a production write from Tasks 2-8).

Run: `grep -rln "ProjectState(" services/ --include=*.py`
Expected: no results outside `services/orchestrator/graph/state.py` itself (confirms nothing anywhere constructs a raw `ProjectState(...)` literal that would need updating for the three new required keys — every test and node goes through `new_project_state()`).

- [ ] **Step 3: Commit**

```bash
git add TODO.md
git commit -m "$(cat <<'EOF'
docs: check off context/compaction strategy in TODO.md Phase 22
EOF
)"
```
