# Per-Task Structured Progress Tracking Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace `task_queue`'s implicit "completion = removal from the list" semantics with an explicit `status` (`pending`/`in_progress`/`done`/`blocked`) per task, remove the redundant `current_task` field, and surface real per-task progress through the Orchestrator's HTTP API and the Telegram Gateway.

**Architecture:** `task_queue` becomes the complete, ordered list of every task for the current implementation phase, for that phase's lifetime — nothing is ever removed. `node_coder` finds the active task by scanning for `status == "in_progress"` instead of promoting `task_queue[0]` into a separate `current_task` dict. `GET /status/{project_id}` and the Telegram `/status`-equivalent path gain a `task_progress` summary. The Memory Curator's input gains a per-task status line alongside its existing chat-message excerpt.

**Tech Stack:** Python 3.12, LangGraph `TypedDict` state, Pydantic v2, FastAPI, aiogram, pytest.

## Global Constraints

- No inline comments; self-documenting names (CLAUDE.md).
- Type hints everywhere; no bare `Any` without a comment explaining why (CLAUDE.md).
- Test naming: `test_<thing_under_test>_<condition>_<expected_outcome>` (`.claude/rules/testing.md`).
- **Sandbox gap:** `langgraph`, `langchain_core`, `langchain_anthropic`, `langchain_ollama`, `aiogram`, `pyyaml`, and `ruff` are not installed in this execution sandbox, and host installs are blocked by this repo's own pre-tool-use hooks. Every test file touched by this plan imports at least one of `langchain_core`/`langgraph`/`aiogram` transitively, so `pytest` cannot actually execute for real here. For every test-running step below: attempt the `pytest` command exactly as written; if it fails with `ModuleNotFoundError` for one of the packages above, that confirms the known permanent gap (not a real failure) — hand-trace the test body against the implementation instead (read the test, read the code, confirm line-by-line the assertion would hold), and say explicitly in the task's commit message and review notes which verification mode was used (real run vs. hand-trace). Do not attempt `pip install` to fix it.
- No new dependencies are needed anywhere in this plan.
- Work happens directly on `release/v1` — no feature branch or worktree (established project convention).
- Do **not** tag `phase-22-done` or claim "Phase 22 complete" at the end of this plan — Phase 22 is a backlog of 10 independent stretch items in TODO.md and this plan implements exactly one of them (per-task progress tracking). Only check off that single TODO.md bullet.

---

### Task 1: Document the schema change in ARCHITECTURE.md

Per TODO.md's Phase 22 preamble ("Each item should get an `ARCHITECTURE.md` update (new ADR) before implementation starts"), the doc update lands first, describing the target end-state the following tasks implement.

**Files:**
- Modify: `ARCHITECTURE.md:299-329` (§5 State Schema)
- Modify: `ARCHITECTURE.md:556` (§10 Decision Log — insert new row after ADR-033)

**Interfaces:**
- Produces: the documented target shape `{"description": str, "status": "pending" | "in_progress" | "done" | "blocked", "attempts": int}` for each `task_queue` item, and the documented removal of `current_task` — every later task's code must match this exactly.

- [ ] **Step 1: Replace the §5 `ProjectState` code block and its trailing prose**

Read `ARCHITECTURE.md:299-329` first to confirm the exact current text, then replace the whole block (the ` ```python ... ``` ` block plus the one-sentence paragraph immediately after it) with:

````markdown
```python
class ProjectState(TypedDict):
    project_id: str
    mode: Literal["greenfield", "brownfield"]
    stack: Literal["python", "node"]
    phase: Literal[
        "intake", "planning", "roasting", "awaiting_plan_approval",
        "implementing", "reviewing", "testing", "awaiting_cost_approval",
        "awaiting_merge_approval", "pr_pending", "done", "blocked",
    ]
    plan: dict
    messages: Annotated[list[BaseMessage], add]
    task_queue: list[dict]
    cost_usd_session: float
    cost_usd_weekly: float
    coverage_pct: float | None
    coverage_threshold: int
    pr_mode: Literal["auto", "manual", "configurable"]
    coverage_gate_mode: Literal["hard", "soft", "configurable"]
    git_branch: str | None
    git_remote_url: str | None
    curator_source: str | None
    pending_approval: dict | None
```

`pending_approval` is what gets attached to the `interrupt()` payload — the Gateway reads it to render the right Telegram message (plan text + approve/reject buttons, or "cost threshold reached: $42.10 of $50/week — continue?").

Each `task_queue` item is `{"description": str, "status": "pending" | "in_progress" | "done" | "blocked", "attempts": int}` (ADR-034). Every task for the current implementation phase stays in the list for its lifetime — nothing is removed on completion — so a resumed session or a `/status` query can see the full per-task history, not just whichever task is currently active. There is no separate `current_task` field: the active task (at most one at a time) is whichever item has `status == "in_progress"`.
````

Note this also fixes pre-existing drift: the prior block was missing `stack`, `git_branch`, `git_remote_url`, and `curator_source` entirely, even though they already existed in the real `ProjectState`.

- [ ] **Step 2: Add ADR-034 to the §10 Decision Log**

Insert a new table row immediately after the ADR-033 row (`ARCHITECTURE.md:556`):

```markdown
| 034 | `task_queue` items carry an explicit `status` ("pending"/"in_progress"/"done"/"blocked") and `attempts` count, and are never removed from the list on completion; the separate `current_task` field is deleted from `ProjectState` (Phase 22 stretch) | `task_queue` previously tracked completion only by list removal (`task_queue[1:]`), with the active item duplicated into a free-form `current_task` dict carrying an ad hoc `attempts` key. This meant a permanently-blocked task (attempts exhausted) was left sitting in both `current_task` *and* unchanged in `task_queue[0]` forever — two copies of one logical entity with no shared status. A single in-place status list is both the fix for that duplication bug and what actually satisfies Anthropic's published guidance on long-running coding-agent harnesses: a persistent, machine-readable task list with per-item status, readable by a resumed session or a `/status` query (TODO.md Phase 22) |
```

- [ ] **Step 3: Commit**

```bash
git add ARCHITECTURE.md
git commit -m "$(cat <<'EOF'
docs(orchestrator): document per-task status in ProjectState (ADR-034)
EOF
)"
```

---

### Task 2: `TaskStatus` literal and `current_task` removal in `state.py`

**Files:**
- Modify: `services/orchestrator/graph/state.py`
- Test: `services/orchestrator/graph/tests/unit/test_state.py`

**Interfaces:**
- Produces: `TaskStatus = Literal["pending", "in_progress", "done", "blocked"]` in `services.orchestrator.graph.state`, documenting the valid `status` values every `task_queue` item's `status` key takes (referenced by ARCHITECTURE.md §5/ADR-034 from Task 1; not imported by later tasks' code, which compares status values as plain string literals — see Task 3). `ProjectState` no longer has a `current_task` key. `new_project_state()` no longer sets `current_task`.

- [ ] **Step 1: Read the current test to see what changes**

`services/orchestrator/graph/tests/unit/test_state.py:18-19` currently reads:

```python
    assert state["task_queue"] == []
    assert state["current_task"] is None
```

- [ ] **Step 2: Update the test — drop the `current_task` assertion**

Edit `services/orchestrator/graph/tests/unit/test_state.py:18-19` to:

```python
    assert state["task_queue"] == []
```

- [ ] **Step 3: Run it to confirm it still passes structurally (hand-trace if blocked)**

Run: `pytest services/orchestrator/graph/tests/unit/test_state.py -v`
Expected: `PASS` (or, per Global Constraints, a `ModuleNotFoundError` for `langchain_core` — in that case hand-trace: `new_project_state()` in the *current*, not-yet-modified `state.py` still sets `task_queue=[]`, so this assertion already holds before Step 4 below).

- [ ] **Step 4: Add `TaskStatus` and remove `current_task` from `state.py`**

In `services/orchestrator/graph/state.py`, change the literal-type block (currently lines 8-26) by adding one new line after the existing `Stack` literal:

```python
Stack = Literal["python", "node"]
TaskStatus = Literal["pending", "in_progress", "done", "blocked"]
```

Remove the `current_task: dict | None` field from the `ProjectState` class body (currently line 44):

```python
    task_queue: list[dict]
```

(i.e. delete the `current_task: dict | None` line that followed it).

Remove the `current_task=None,` line from `new_project_state()`'s return statement (currently line 113):

```python
        task_queue=[],
```

(i.e. delete the `current_task=None,` line that followed it).

- [ ] **Step 5: Run the test again to confirm it passes (hand-trace if blocked)**

Run: `pytest services/orchestrator/graph/tests/unit/test_state.py -v`
Expected: `PASS` (or hand-trace: `ProjectState` no longer declares `current_task`, `new_project_state()` no longer sets it, and the test no longer asserts on it — consistent).

- [ ] **Step 6: Commit**

```bash
git add services/orchestrator/graph/state.py services/orchestrator/graph/tests/unit/test_state.py
git commit -m "$(cat <<'EOF'
feat(orchestrator): add TaskStatus literal, remove current_task from ProjectState

EOF
)"
```

---

### Task 3: Rewrite `node_coder`/`node_task_decomposition` to use in-place task status

**Files:**
- Modify: `services/orchestrator/graph/nodes.py`
- Test: `services/orchestrator/graph/tests/unit/test_nodes.py`

**Interfaces:**
- Consumes: `CoderTask.description` (unchanged, from `services/orchestrator/agents/coder.py` — `CoderTask` itself is **not** modified in this plan; its LLM-facing schema stays exactly `{"description": str}` so the decomposition model is never asked to produce a status/attempts value).
- Produces: `node_task_decomposition` returns `task_queue` items shaped `{"description": str, "status": "pending", "attempts": 0}`. `node_coder` returns updated `task_queue` items shaped `{"description": str, "status": "pending" | "in_progress" | "done" | "blocked", "attempts": int}`, no `current_task` key anywhere in its return value. `task_queue` stays typed as `list[dict]` on `ProjectState` (Task 2's `TaskStatus` literal documents the valid `status` values but isn't imported here — nothing in this task's code takes a `TaskStatus`-typed parameter, only plain dicts with string-literal comparisons, matching how `state["phase"]` is already compared elsewhere in this module).

- [ ] **Step 1: Read the current tests to see every site that needs rewriting**

`services/orchestrator/graph/tests/unit/test_nodes.py:195-264` currently has 6 tests touching `task_queue`/`current_task`:

```python
def test_node_task_decomposition_populates_task_queue():
    router = FakeRouter([ONE_TASK])
    state = new_project_state("proj-1")
    state["plan"] = {"summary": "A minimal test project."}

    update = node_task_decomposition(state, router)

    assert update["phase"] == "implementing"
    assert update["task_queue"] == [{"description": "Do the trivial thing"}]


def test_node_coder_returns_reviewing_phase_when_queue_already_empty():
    state = new_project_state("proj-1")
    state["task_queue"] = []

    update = node_coder(state, router=None, executor_client=None)

    assert update == {"phase": "reviewing"}


def test_node_coder_succeeds_and_advances_to_reviewing_when_last_task_done():
    router = FakeRouter([CODER_DONE])
    state = new_project_state("proj-1")
    state["task_queue"] = [{"description": "Do the trivial thing"}]

    update = node_coder(state, router, executor_client=_RecordingExecutorClient())

    assert update["task_queue"] == []
    assert update["current_task"] is None
    assert update["phase"] == "reviewing"


def test_node_coder_keeps_implementing_when_more_tasks_remain():
    router = FakeRouter([CODER_DONE])
    state = new_project_state("proj-1")
    state["task_queue"] = [{"description": "First task"}, {"description": "Second task"}]

    update = node_coder(state, router, executor_client=_RecordingExecutorClient())

    assert update["task_queue"] == [{"description": "Second task"}]
    assert update["current_task"] is None
    assert update["phase"] == "implementing"


def test_node_coder_retries_a_failed_task_below_the_attempt_limit():
    router = FakeRouter([_tool_call("read_file", {"path": "missing.py"})] * 10)
    state = new_project_state("proj-1")
    state["task_queue"] = [{"description": "Do the trivial thing"}]

    update = node_coder(state, router, executor_client=_RecordingExecutorClient())

    assert update["phase"] == "implementing"
    assert update["current_task"]["attempts"] == 1
    assert update["current_task"]["description"] == "Do the trivial thing"


def test_node_coder_blocks_after_exhausting_the_attempt_budget():
    router = FakeRouter([_tool_call("read_file", {"path": "missing.py"})] * 10)
    state = new_project_state("proj-1")
    state["task_queue"] = [{"description": "Do the trivial thing"}]
    state["current_task"] = {
        "description": "Do the trivial thing",
        "attempts": MAX_TASK_ATTEMPTS - 1,
    }

    update = node_coder(state, router, executor_client=_RecordingExecutorClient())

    assert update["phase"] == "blocked"
    assert update["curator_source"] == "coder"
    assert update["current_task"]["attempts"] == MAX_TASK_ATTEMPTS
```

- [ ] **Step 2: Replace all 6 tests with the new-shape versions, plus 2 new tests**

Replace the entire block quoted in Step 1 (`test_nodes.py:195-264`) with:

```python
def test_node_task_decomposition_populates_task_queue():
    router = FakeRouter([ONE_TASK])
    state = new_project_state("proj-1")
    state["plan"] = {"summary": "A minimal test project."}

    update = node_task_decomposition(state, router)

    assert update["phase"] == "implementing"
    assert update["task_queue"] == [
        {"description": "Do the trivial thing", "status": "pending", "attempts": 0}
    ]


def test_node_coder_returns_reviewing_phase_when_queue_already_empty():
    state = new_project_state("proj-1")
    state["task_queue"] = []

    update = node_coder(state, router=None, executor_client=None)

    assert update == {"phase": "reviewing"}


def test_node_coder_returns_reviewing_phase_when_every_task_is_already_done():
    state = new_project_state("proj-1")
    state["task_queue"] = [
        {"description": "First task", "status": "done", "attempts": 1}
    ]

    update = node_coder(state, router=None, executor_client=None)

    assert update == {"phase": "reviewing"}


def test_node_coder_succeeds_and_advances_to_reviewing_when_last_task_done():
    router = FakeRouter([CODER_DONE])
    state = new_project_state("proj-1")
    state["task_queue"] = [
        {"description": "Do the trivial thing", "status": "pending", "attempts": 0}
    ]

    update = node_coder(state, router, executor_client=_RecordingExecutorClient())

    assert update["task_queue"] == [
        {"description": "Do the trivial thing", "status": "done", "attempts": 0}
    ]
    assert update["phase"] == "reviewing"


def test_node_coder_keeps_implementing_when_more_tasks_remain():
    router = FakeRouter([CODER_DONE])
    state = new_project_state("proj-1")
    state["task_queue"] = [
        {"description": "First task", "status": "pending", "attempts": 0},
        {"description": "Second task", "status": "pending", "attempts": 0},
    ]

    update = node_coder(state, router, executor_client=_RecordingExecutorClient())

    assert update["task_queue"] == [
        {"description": "First task", "status": "done", "attempts": 0},
        {"description": "Second task", "status": "pending", "attempts": 0},
    ]
    assert update["phase"] == "implementing"


def test_node_coder_resumes_the_in_progress_task_rather_than_the_first_pending_one():
    router = FakeRouter([CODER_DONE])
    state = new_project_state("proj-1")
    state["task_queue"] = [
        {"description": "First task", "status": "done", "attempts": 0},
        {"description": "Second task", "status": "in_progress", "attempts": 1},
        {"description": "Third task", "status": "pending", "attempts": 0},
    ]

    update = node_coder(state, router, executor_client=_RecordingExecutorClient())

    assert update["task_queue"] == [
        {"description": "First task", "status": "done", "attempts": 0},
        {"description": "Second task", "status": "done", "attempts": 1},
        {"description": "Third task", "status": "pending", "attempts": 0},
    ]
    assert update["phase"] == "implementing"


def test_node_coder_retries_a_failed_task_below_the_attempt_limit():
    router = FakeRouter([_tool_call("read_file", {"path": "missing.py"})] * 10)
    state = new_project_state("proj-1")
    state["task_queue"] = [
        {"description": "Do the trivial thing", "status": "pending", "attempts": 0}
    ]

    update = node_coder(state, router, executor_client=_RecordingExecutorClient())

    assert update["phase"] == "implementing"
    assert update["task_queue"] == [
        {"description": "Do the trivial thing", "status": "in_progress", "attempts": 1}
    ]


def test_node_coder_blocks_after_exhausting_the_attempt_budget():
    router = FakeRouter([_tool_call("read_file", {"path": "missing.py"})] * 10)
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
    assert update["curator_source"] == "coder"
    assert update["task_queue"] == [
        {
            "description": "Do the trivial thing",
            "status": "blocked",
            "attempts": MAX_TASK_ATTEMPTS,
        }
    ]
```

- [ ] **Step 3: Run the tests to confirm they fail against the current implementation**

Run: `pytest services/orchestrator/graph/tests/unit/test_nodes.py -v -k "node_coder or node_task_decomposition"`
Expected: `FAIL` — the new tests assert on a `status`/`attempts`-shaped `task_queue` and no `current_task` key, which the current `node_coder`/`node_task_decomposition` don't produce yet. (Or, per Global Constraints, a `ModuleNotFoundError` for `langchain_core`/`langgraph` — in that case hand-trace: re-read the *current* `node_coder` body at `services/orchestrator/graph/nodes.py:248-308` and confirm it still returns `current_task`/plain-description items, which would fail these new assertions.)

- [ ] **Step 4: Rewrite `node_task_decomposition` and `node_coder`**

In `services/orchestrator/graph/nodes.py`, leave the existing `from services.orchestrator.graph.state import ProjectState` import (currently line 90) unchanged — nothing in this task's code needs a `TaskStatus`-typed parameter.

Replace `node_task_decomposition` (currently `nodes.py:236-245`) with:

```python
def node_task_decomposition(state: ProjectState, router: ModelRouter) -> dict[str, Any]:
    """Breaks the approved plan into task_queue, once, right after plan approval."""
    tasks, ai_message = asyncio.run(decompose_plan_into_tasks(router, state["plan"]))
    cost = router.cost_usd(CODER_ROLE, ai_message.usage_metadata)
    task_queue = [
        {"description": task.description, "status": "pending", "attempts": 0} for task in tasks
    ]
    logger.info("project %s: decomposed into %d task(s)", state["project_id"], len(task_queue))
    return _with_cost(
        state,
        {"task_queue": task_queue, "phase": "implementing"},
        cost,
    )
```

Replace `node_coder` and its docstring, plus `route_after_coder` stays untouched immediately below it (currently `nodes.py:248-318`, replace through line 308 only — stop before `route_after_coder`) with:

```python
def _replace_task(task_queue: list[dict], index: int, task: dict) -> list[dict]:
    """Returns a new task_queue list with the item at `index` replaced by `task`."""
    return [task if i == index else existing for i, existing in enumerate(task_queue)]


def node_coder(
    state: ProjectState, router: ModelRouter, executor_client: ExecutorClient
) -> dict[str, Any]:
    """Implements one task-or-retry-attempt per execution.

    No `interrupt()` call happens anywhere in this node (the Coder is fully
    autonomous within its retry budget), so -- unlike the Planner/cost/merge
    gates -- there's no LangGraph replay concern here: this node can safely
    make LLM and Executor calls directly. Every task_queue item carries its
    own `status` and `attempts` count (ADR-034); the active task is whichever
    item has status "in_progress" (promoted from the first "pending" item if
    none is active yet), rather than a separate `current_task` field.
    """
    task_queue = state["task_queue"]
    active_index = next(
        (i for i, task in enumerate(task_queue) if task["status"] == "in_progress"), None
    )
    if active_index is None:
        active_index = next(
            (i for i, task in enumerate(task_queue) if task["status"] == "pending"), None
        )
        if active_index is None:
            return {"phase": "reviewing"}

    active_task = {**task_queue[active_index], "status": "in_progress"}
    result = asyncio.run(
        run_coder_task(
            router,
            executor_client,
            state["project_id"],
            active_task["description"],
            stack=state["stack"],
        )
    )

    if result.success:
        active_task["status"] = "done"
        new_queue = _replace_task(task_queue, active_index, active_task)
        remaining = sum(1 for task in new_queue if task["status"] in ("pending", "in_progress"))
        logger.info(
            "project %s: task succeeded, %d remaining", state["project_id"], remaining
        )
        return _with_cost(
            state,
            {
                "task_queue": new_queue,
                "phase": "implementing" if remaining else "reviewing",
            },
            result.cost_usd,
        )

    active_task["attempts"] += 1
    if active_task["attempts"] >= MAX_TASK_ATTEMPTS:
        active_task["status"] = "blocked"
        logger.warning(
            "project %s: task exhausted %d attempts, blocking",
            state["project_id"],
            active_task["attempts"],
        )
        new_queue = _replace_task(task_queue, active_index, active_task)
        return _with_cost(
            state,
            {"task_queue": new_queue, "phase": "blocked", "curator_source": "coder"},
            result.cost_usd,
        )

    logger.info(
        "project %s: task attempt %d failed, retrying",
        state["project_id"],
        active_task["attempts"],
    )
    new_queue = _replace_task(task_queue, active_index, active_task)
    return _with_cost(
        state, {"task_queue": new_queue, "phase": "implementing"}, result.cost_usd
    )
```

`node_coder` compares against string literals directly, matching every other `Literal`-typed field's usage in this module (e.g. `state["phase"] == "blocked"`).

- [ ] **Step 5: Run the tests to confirm they pass (hand-trace if blocked)**

Run: `pytest services/orchestrator/graph/tests/unit/test_nodes.py -v -k "node_coder or node_task_decomposition"`
Expected: `PASS` (or hand-trace each test against the new `node_coder`/`node_task_decomposition` bodies above, confirming each assertion holds).

- [ ] **Step 6: Run the full `test_nodes.py` file to catch any other breakage**

Run: `pytest services/orchestrator/graph/tests/unit/test_nodes.py -v`
Expected: `PASS` for every test (or hand-trace: grep the file for any other `current_task`/raw-`task_queue`-literal reference beyond the block replaced in Step 2 — there should be none left; the earlier research pass over this repo found exactly the 6 tests replaced above).

- [ ] **Step 7: Commit**

```bash
git add services/orchestrator/graph/nodes.py services/orchestrator/graph/tests/unit/test_nodes.py
git commit -m "$(cat <<'EOF'
feat(orchestrator): track task_queue status in place, drop current_task

EOF
)"
```

---

### Task 4: Feed task outcomes into the Memory Curator

**Files:**
- Modify: `services/orchestrator/graph/nodes.py`
- Test: `services/orchestrator/graph/tests/unit/test_nodes.py`

**Interfaces:**
- Consumes: `state["task_queue"]` (Task 3's shape: `list[dict]` of `{"description": str, "status": "pending" | "in_progress" | "done" | "blocked", "attempts": int}`).
- Produces: `_task_progress_lines(task_queue: list[dict]) -> list[str]`, a private helper in `nodes.py`, consumed only within this task's change to `node_memory_curator`.

- [ ] **Step 1: Write the failing test for the new helper**

Add to `services/orchestrator/graph/tests/unit/test_nodes.py`, near the other `node_memory_curator`-adjacent tests (after `test_node_memory_curator_is_a_noop_without_a_curator_source`, currently ending at line 524):

```python
def test_task_progress_lines_renders_each_task_with_status_and_attempts():
    from services.orchestrator.graph.nodes import _task_progress_lines

    task_queue = [
        {"description": "Add a User model", "status": "done", "attempts": 1},
        {"description": "Add a POST /login endpoint", "status": "blocked", "attempts": 3},
    ]

    lines = _task_progress_lines(task_queue)

    assert lines == [
        "[done] Add a User model (attempts: 1)",
        "[blocked] Add a POST /login endpoint (attempts: 3)",
    ]


def test_task_progress_lines_returns_empty_list_for_empty_queue():
    from services.orchestrator.graph.nodes import _task_progress_lines

    assert _task_progress_lines([]) == []
```

- [ ] **Step 2: Run it to verify it fails**

Run: `pytest services/orchestrator/graph/tests/unit/test_nodes.py -v -k test_task_progress_lines`
Expected: `FAIL` with `ImportError: cannot import name '_task_progress_lines'` (or, per Global Constraints, `ModuleNotFoundError` for `langchain_core`/`langgraph` first — in that case hand-trace: `_task_progress_lines` does not exist yet in `nodes.py`, so the import in the test would fail).

- [ ] **Step 3: Add `_task_progress_lines` and wire it into `node_memory_curator`**

In `services/orchestrator/graph/nodes.py`, add this function near `_with_cost` (after its definition, currently ending at line 110):

```python
def _task_progress_lines(task_queue: list[dict]) -> list[str]:
    """Renders each task_queue item as a one-line status summary for the
    Memory Curator's input, e.g. "[done] Add a User model (attempts: 1)"."""
    return [
        f"[{task['status']}] {task['description']} (attempts: {task['attempts']})"
        for task in task_queue
    ]
```

In `node_memory_curator` (currently `nodes.py:672-680`), change:

```python
    project_id = state["project_id"]
    phase_events = [msg.content for msg in state["messages"][-20:]]
    if not phase_events:
```

to:

```python
    project_id = state["project_id"]
    phase_events = [msg.content for msg in state["messages"][-20:]] + _task_progress_lines(
        state["task_queue"]
    )
    if not phase_events:
```

- [ ] **Step 4: Run the new tests to verify they pass**

Run: `pytest services/orchestrator/graph/tests/unit/test_nodes.py -v -k test_task_progress_lines`
Expected: `PASS` (or hand-trace: `_task_progress_lines` now exists and its list-comprehension body matches both test cases exactly).

- [ ] **Step 5: Run the full memory curator test suite to confirm no regression**

Run: `pytest services/orchestrator/graph/tests/unit/test_memory_curator.py -v`
Expected: `PASS` for every test (or hand-trace: every existing test in this file uses `new_project_state(...)`, whose `task_queue` defaults to `[]` unless a test explicitly sets it — none of them do — so `_task_progress_lines(state["task_queue"])` evaluates to `[]` in every existing test, meaning `phase_events` is unchanged from before for all of them; `test_node_memory_curator_skips_when_no_events` in particular still sees an empty `phase_events` and still skips).

- [ ] **Step 6: Commit**

```bash
git add services/orchestrator/graph/nodes.py services/orchestrator/graph/tests/unit/test_nodes.py
git commit -m "$(cat <<'EOF'
feat(orchestrator): feed task_queue outcomes into Memory Curator input

EOF
)"
```

---

### Task 5: Surface `task_progress` in the Orchestrator's HTTP API

**Files:**
- Modify: `services/orchestrator/service.py`
- Modify: `services/orchestrator/api.py`
- Test: `services/orchestrator/tests/unit/test_service.py`
- Test: `services/orchestrator/tests/unit/test_api.py`

**Interfaces:**
- Consumes: `snapshot.values["task_queue"]` (Task 3's shape).
- Produces: `OrchestratorService.status()`'s returned dict gains a `task_progress` key: `None` when `task_queue` is empty, else `{"done": int, "total": int, "tasks": list[dict]}`. `ProjectStatusResponse` gains a matching `task_progress: dict | None` field.

- [ ] **Step 1: Write the failing tests**

Add to `services/orchestrator/tests/unit/test_service.py`, after `test_status_unknown_project_raises` (currently ending at line 75):

```python
def test_status_before_decomposition_has_no_task_progress(service):
    result = service.handle_webhook("my-project", "hello")

    assert result["phase"] == "awaiting_plan_approval"
    assert result["task_progress"] is None


def test_status_after_plan_approval_includes_task_progress(service):
    service.handle_webhook("my-project", "hello")

    result = service.resume("my-project", "approve")

    assert result["phase"] == "awaiting_merge_approval"
    assert result["task_progress"] == {
        "done": 1,
        "total": 1,
        "tasks": [{"description": "Do the trivial thing", "status": "done", "attempts": 0}],
    }
```

Add to `services/orchestrator/tests/unit/test_api.py`, after `test_status_known_project_returns_current_phase` (currently ending at line 89):

```python
def test_status_includes_task_progress_once_tasks_exist(client):
    client.post("/webhook", json={"user_id": 1, "text": "hi", "project_id": "my-project"})
    client.post("/resume/my-project", json={"decision": "approve"})

    response = client.get("/status/my-project")

    body = response.json()
    assert body["task_progress"] == {
        "done": 1,
        "total": 1,
        "tasks": [{"description": "Do the trivial thing", "status": "done", "attempts": 0}],
    }
```

- [ ] **Step 2: Run them to verify they fail**

Run: `pytest services/orchestrator/tests/unit/test_service.py services/orchestrator/tests/unit/test_api.py -v -k task_progress`
Expected: `FAIL` with a `KeyError`/`AssertionError` since neither `status()` nor `ProjectStatusResponse` produce `task_progress` yet (or, per Global Constraints, `ModuleNotFoundError` for `langgraph`/`fastapi`'s transitive chain — in that case hand-trace: `OrchestratorService.status()`'s current return dict at `service.py:134-138` has exactly three keys, none named `task_progress`, so `result["task_progress"]` would raise `KeyError`).

- [ ] **Step 3: Add `task_progress` to `OrchestratorService.status()`**

In `services/orchestrator/service.py`, replace the `status()` method body (currently lines 128-138):

```python
        snapshot = self._graph.get_state(self._config(project_id))
        if not snapshot.values:
            raise UnknownProjectError(project_id)
        phase = snapshot.values["phase"]
        if phase in _TERMINAL_PHASES:
            clear_progress(project_id)
        task_queue = snapshot.values["task_queue"]
        task_progress = (
            {
                "done": sum(1 for task in task_queue if task["status"] == "done"),
                "total": len(task_queue),
                "tasks": task_queue,
            }
            if task_queue
            else None
        )
        return {
            "project_id": project_id,
            "phase": phase,
            "pending_approval": snapshot.values["pending_approval"],
            "task_progress": task_progress,
        }
```

Also update the method's docstring `Returns:` line (currently line 123) from:

```
            A dict with `project_id`, `phase`, and `pending_approval`.
```

to:

```
            A dict with `project_id`, `phase`, `pending_approval`, and
            `task_progress` (None before task decomposition has run, else
            `{"done": int, "total": int, "tasks": list[dict]}`).
```

- [ ] **Step 4: Add `task_progress` to `ProjectStatusResponse`**

In `services/orchestrator/api.py`, change the `ProjectStatusResponse` class (currently lines 24-29):

```python
class ProjectStatusResponse(BaseModel):
    """A project's current phase and any pending approval, rendered by the Gateway."""

    project_id: str
    phase: str
    pending_approval: dict | None
    task_progress: dict | None
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `pytest services/orchestrator/tests/unit/test_service.py services/orchestrator/tests/unit/test_api.py -v -k task_progress`
Expected: `PASS` (or hand-trace: `fast_track_router(rounds=2)` scripts exactly `ONE_TASK` — `CoderTask(description="Do the trivial thing")` — through decomposition, then `CODER_DONE` succeeds it on the first `node_coder` call within the same `resume()` invocation, landing `task_queue` at `[{"description": "Do the trivial thing", "status": "done", "attempts": 0}]` before the graph reaches the `awaiting_merge_approval` interrupt — matching both new assertions exactly).

- [ ] **Step 6: Run the full two test files to confirm no regression**

Run: `pytest services/orchestrator/tests/unit/test_service.py services/orchestrator/tests/unit/test_api.py -v`
Expected: `PASS` for every test (or hand-trace: every other existing test in both files only reads `phase`/`project_id`/`pending_approval`/`project_ids`/`chunks` keys, never asserts the full dict via `==` against a literal missing `task_progress`, so adding the new key is additive and doesn't break any of them — confirmed by reading both files in full during planning).

- [ ] **Step 7: Commit**

```bash
git add services/orchestrator/service.py services/orchestrator/api.py services/orchestrator/tests/unit/test_service.py services/orchestrator/tests/unit/test_api.py
git commit -m "$(cat <<'EOF'
feat(orchestrator): surface task_progress on GET /status/{project_id}

EOF
)"
```

---

### Task 6: Render task progress in the Telegram Gateway

**Files:**
- Modify: `services/telegram_gateway/rendering.py`
- Modify: `services/telegram_gateway/bot.py`
- Test: `services/telegram_gateway/tests/unit/test_rendering.py`
- Test: `services/telegram_gateway/tests/unit/test_bot_handlers.py`

**Interfaces:**
- Consumes: a webhook/status JSON response's `task_progress` field, shaped `{"done": int, "total": int, "tasks": [{"description": str, "status": str, "attempts": int}, ...]}` (Task 5).
- Produces: `render_task_progress(task_progress: dict) -> str` in `rendering.py`, consumed by `bot.py`'s `_relay_webhook_result`. This is the actual code path Telegram's `/status` command exercises today (it forwards through the same generic webhook-response renderer as `/new` and free text — there is no separate status-specific handler in `bot.py`).

- [ ] **Step 1: Write the failing rendering test**

Add to `services/telegram_gateway/tests/unit/test_rendering.py`:

```python
from services.telegram_gateway.rendering import render_approval, render_task_progress


def test_render_task_progress_shows_done_count_and_in_progress_task():
    text = render_task_progress(
        {
            "done": 1,
            "total": 3,
            "tasks": [
                {"description": "First task", "status": "done", "attempts": 1},
                {"description": "Second task", "status": "in_progress", "attempts": 0},
                {"description": "Third task", "status": "pending", "attempts": 0},
            ],
        }
    )

    assert text == "1/3 tasks done\nIn progress: Second task"


def test_render_task_progress_shows_blocked_task_instead_of_in_progress():
    text = render_task_progress(
        {
            "done": 0,
            "total": 1,
            "tasks": [{"description": "Do the thing", "status": "blocked", "attempts": 3}],
        }
    )

    assert text == "0/1 tasks done\nBlocked: Do the thing"


def test_render_task_progress_shows_only_the_count_when_all_tasks_are_done():
    text = render_task_progress(
        {
            "done": 1,
            "total": 1,
            "tasks": [{"description": "Do the thing", "status": "done", "attempts": 1}],
        }
    )

    assert text == "1/1 tasks done"
```

(Change the existing `from services.telegram_gateway.rendering import render_approval` line at the top of the file to the combined import shown above, rather than adding a second import line.)

- [ ] **Step 2: Run it to verify it fails**

Run: `pytest services/telegram_gateway/tests/unit/test_rendering.py -v -k render_task_progress`
Expected: `FAIL` with `ImportError: cannot import name 'render_task_progress'` (or, per Global Constraints, `ModuleNotFoundError` for `aiogram` — in that case hand-trace: `render_task_progress` does not exist yet in `rendering.py`).

- [ ] **Step 3: Implement `render_task_progress`**

In `services/telegram_gateway/rendering.py`, add after `render_approval` (currently ending at line 42):

```python
def render_task_progress(task_progress: dict[str, Any]) -> str:
    """Renders a ProjectStatusResponse's task_progress field as a status line.

    Args:
        task_progress: {"done": int, "total": int, "tasks": [{"description",
            "status", "attempts"}, ...]} (services/orchestrator/api.py).

    Returns:
        A one- or two-line summary, e.g. "1/3 tasks done\\nIn progress: ..."
        -- a blocked task takes priority over an in-progress one for the
        second line, since a blocked task means the phase has actually halted.
    """
    lines = [f"{task_progress['done']}/{task_progress['total']} tasks done"]
    blocked = next(
        (task for task in task_progress["tasks"] if task["status"] == "blocked"), None
    )
    active = next(
        (task for task in task_progress["tasks"] if task["status"] == "in_progress"), None
    )
    if blocked:
        lines.append(f"Blocked: {blocked['description']}")
    elif active:
        lines.append(f"In progress: {active['description']}")
    return "\n".join(lines)
```

- [ ] **Step 4: Run the rendering tests to verify they pass**

Run: `pytest services/telegram_gateway/tests/unit/test_rendering.py -v`
Expected: `PASS` for every test (or hand-trace each of the 3 new cases against the implementation above, plus the 2 pre-existing `render_approval` tests, which are untouched).

- [ ] **Step 5: Write the failing bot-handler tests**

Add to `services/telegram_gateway/tests/unit/test_bot_handlers.py`, after `test_handle_command_sends_plain_acknowledgment_when_no_gate_is_pending` (currently ending at line 101):

```python
def test_handle_command_renders_task_progress_when_no_gate_is_pending(
    message, orchestrator, poller
):
    response = MagicMock()
    response.json.return_value = {
        "project_id": "proj-1",
        "phase": "implementing",
        "pending_approval": None,
        "task_progress": {
            "done": 1,
            "total": 3,
            "tasks": [
                {"description": "First task", "status": "done", "attempts": 1},
                {"description": "Second task", "status": "in_progress", "attempts": 0},
                {"description": "Third task", "status": "pending", "attempts": 0},
            ],
        },
    }
    orchestrator.send_webhook = AsyncMock(return_value=response)

    asyncio.run(handle_command(message, orchestrator, poller))

    message.answer.assert_awaited_once_with("1/3 tasks done\nIn progress: Second task")


def test_handle_command_falls_back_to_plain_acknowledgment_without_task_progress(
    message, orchestrator, poller
):
    """The original webhook fixture has no task_progress key at all -- confirms
    the new branch doesn't break the pre-existing no-tasks-yet case."""
    asyncio.run(handle_command(message, orchestrator, poller))

    message.answer.assert_awaited_once_with("Got it -- working on that.")
```

- [ ] **Step 6: Run them to verify the new one fails**

Run: `pytest services/telegram_gateway/tests/unit/test_bot_handlers.py -v -k task_progress`
Expected: `test_handle_command_renders_task_progress_when_no_gate_is_pending` `FAIL`s (current `_relay_webhook_result` always answers with the plain acknowledgment when `pending_approval` is falsy); `test_handle_command_falls_back_to_plain_acknowledgment_without_task_progress` already `PASS`es against the unmodified code. (Or, per Global Constraints, `ModuleNotFoundError` for `aiogram` — in that case hand-trace: `_relay_webhook_result`'s current body at `bot.py:42-55` has no branch reading `task_progress` at all, so the first new test's assertion on a task-progress-shaped message would fail against it.)

- [ ] **Step 7: Wire `task_progress` rendering into `_relay_webhook_result`**

In `services/telegram_gateway/bot.py`, add `render_task_progress` to the existing import from `.rendering` (currently lines 17-22):

```python
from services.telegram_gateway.rendering import (
    CALLBACK_APPROVE,
    CALLBACK_EDIT,
    CALLBACK_REJECT,
    render_approval,
    render_task_progress,
)
```

Replace `_relay_webhook_result` (currently lines 42-55):

```python
async def _relay_webhook_result(message: Message, response) -> None:
    """Renders a webhook response: an Approve/Reject/Edit keyboard if the
    project is now paused at a gate, a task-progress summary if tasks exist,
    a plain acknowledgment otherwise.

    Args:
        message: The incoming message to reply to.
        response: The httpx.Response returned by OrchestratorClient.send_webhook.
    """
    body = response.json()
    pending_approval = body.get("pending_approval")
    if pending_approval:
        text, keyboard = render_approval(pending_approval)
        await message.answer(text, reply_markup=keyboard)
        return
    task_progress = body.get("task_progress")
    if task_progress:
        await message.answer(render_task_progress(task_progress))
        return
    await message.answer("Got it -- working on that.")
```

- [ ] **Step 8: Run the bot-handler tests to verify they pass**

Run: `pytest services/telegram_gateway/tests/unit/test_bot_handlers.py -v`
Expected: `PASS` for every test (or hand-trace: the fixture-based `_webhook_response()` helper used by most existing tests returns a dict with no `task_progress` key at all, so `body.get("task_progress")` evaluates to `None` for all of them, preserving the pre-existing plain-acknowledgment/approval-keyboard behavior; only the two new tests exercise the new branch).

- [ ] **Step 9: Commit**

```bash
git add services/telegram_gateway/rendering.py services/telegram_gateway/bot.py services/telegram_gateway/tests/unit/test_rendering.py services/telegram_gateway/tests/unit/test_bot_handlers.py
git commit -m "$(cat <<'EOF'
feat(gateway): render per-task progress in webhook responses

EOF
)"
```

---

### Task 7: Check off the TODO.md item and final verification pass

**Files:**
- Modify: `TODO.md:451`

**Interfaces:**
- Consumes: nothing new — this task only verifies the prior 6 tasks' combined result and updates the backlog checkbox.

- [ ] **Step 1: Check off the TODO.md bullet**

In `TODO.md`, change line 451 from:

```markdown
- [ ] Per-task structured progress tracking: `task_queue` items are free-form description strings with completion tracked only by removal from the list (no explicit pass/fail/blocked status per task). Anthropic's own published guidance on long-running coding-agent harnesses (see "Effective harnesses for long-running agents") specifically calls out a persistent, machine-readable feature/task list with per-item status as what prevents an agent from losing track of progress across a long session or a restart. Foundry already has the checkpointed `ProjectState` + per-project `PROJECT_MEMORY.md`/`LEARNINGS.md` (Phase 6) covering part of this, but no per-task status field a resumed session or a `/status` Telegram query could read directly
```

to:

```markdown
- [x] Per-task structured progress tracking: `task_queue` items carry an explicit `status` ("pending"/"in_progress"/"done"/"blocked") and `attempts` count, never removed from the list on completion (ADR-034). Surfaced via `GET /status/{project_id}`'s new `task_progress` field and rendered by the Telegram Gateway; also fed into the Memory Curator's input alongside the chat-message excerpt.
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
  services/orchestrator/agents/tests/unit/test_coder.py \
  services/orchestrator/tests/unit/test_service.py \
  services/orchestrator/tests/unit/test_api.py \
  services/telegram_gateway/tests/unit/test_rendering.py \
  services/telegram_gateway/tests/unit/test_bot_handlers.py \
  -v
```

Expected: `PASS` for every test (or, per Global Constraints, `ModuleNotFoundError` — in that case this is the final hand-trace pass: re-read every diff from Tasks 2-6 side by side with each test file's current content one more time, confirming no leftover reference to `current_task` remains anywhere in non-test code — `grep -rn "current_task" services/` should return zero results outside historical git history — and that every assertion added in Tasks 2-6 matches the final implementation exactly).

Run: `grep -rn "current_task" services/ --include=*.py`
Expected: no output (confirms the field was fully removed, not left dangling in some untouched file).

- [ ] **Step 3: Commit**

```bash
git add TODO.md
git commit -m "$(cat <<'EOF'
docs: check off per-task progress tracking in TODO.md Phase 22

EOF
)"
```
