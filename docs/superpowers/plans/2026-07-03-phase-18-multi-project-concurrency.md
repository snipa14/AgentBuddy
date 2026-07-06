# Phase 18 — Multi-Project Concurrency Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** More than one Foundry project can be active at once without `ProjectState` bleeding across `thread_id`s, with local (Ollama) inference serialized to one call at a time and the cost-threshold gate checking a real global weekly total instead of one project's own session cost.

**Architecture:** Replace `node_cost_check`'s per-project `cost_usd_session`-vs-hardcoded-stub check with a global-weekly-total check computed by summing every *other* project's latest checkpointed `cost_usd_weekly` (a new `cost_aggregator.py` helper) plus the current project's own live `cost_usd_weekly`, against a threshold now sourced from `OrchestratorConfig`. Wrap every Ollama-backed model `ModelRouter` constructs in a small duck-typed proxy that serializes calls through a shared `threading.Semaphore(1)` (not `asyncio.Semaphore` — every LLM-backed node bridges to its one async call via its own `asyncio.run()`, each in its own OS thread and event loop, per ADR-019). Add a test proving two projects driven through the same compiled graph never see each other's `plan`/`messages`.

**Tech Stack:** Python 3.12, LangGraph (`StateGraph`, `SqliteSaver`), LangChain (`BaseChatModel`, `ChatAnthropic`, `ChatOllama`), pytest.

## Global Constraints

- No inline comments except non-obvious workarounds with an issue reference; comprehensive Google-style docstrings on every public function/class (CLAUDE.md).
- Type hints everywhere; no bare `Any` without a comment explaining why (CLAUDE.md).
- No bare `except`; always a specific exception type or `Exception` with a log (CLAUDE.md).
- Never reference a model name string directly in agent/router code (CLAUDE.md) — this plan doesn't add any, but don't introduce one while touching `router.py`.
- Coverage floor is 80% (`.claude/rules/testing.md`) — run `pytest --cov=services --cov-fail-under=80 --cov-report=term-missing` before considering the phase done, on a machine with `langgraph`/`langchain-anthropic`/`langchain-ollama`/`fastapi` installed (this sandbox is missing all four, consistent with every phase since Phase 5 — see each task's verification note).
- Test layout: `services/<name>/tests/unit` (no real Docker/network/filesystem beyond `tmp_path`), `services/<name>/tests/integration` for real I/O, marked `@pytest.mark.integration`. This phase only needs unit tests.
- Test naming: `test_<thing_under_test>_<condition>_<expected_outcome>`.
- Conventional Commits format for every commit (`.claude/rules/git.md`); phase-end commit is `feat(phase-18): <description>`.

---

### Task 1: Remove the dead eager import in `graph/__init__.py`

`cost_aggregator.py` (Task 3) needs to be importable by a unit test without `langgraph` installed (this sandbox has none of `langgraph`/`langchain-anthropic`/`langchain-ollama`/`fastapi`). Importing any submodule of `services.orchestrator.graph` first executes `graph/__init__.py`, which currently eagerly imports `build.py` (which imports `langgraph.graph`) — exactly the bug class ADR-030 already fixed once for `services/orchestrator/agents/__init__.py`. A repo-wide grep confirms nothing imports from the `services.orchestrator.graph` package root (`grep -rn "from services.orchestrator.graph import" services` — only submodule imports like `from services.orchestrator.graph.build import build_project_graph` exist), so this is dead code, safe to remove.

**Files:**
- Modify: `services/orchestrator/graph/__init__.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `services.orchestrator.graph` package importable with zero eager submodule imports, so `services.orchestrator.graph.cost_aggregator` (Task 3) can be imported without `langgraph` installed.

- [ ] **Step 1: Confirm nothing imports from the package root**

Run: `grep -rn "from services.orchestrator.graph import\|from services\.orchestrator import graph\b" services --include=*.py`
Expected: no output (already confirmed during planning; re-confirm before deleting).

- [ ] **Step 2: Strip the file to a docstring**

Replace the full contents of `services/orchestrator/graph/__init__.py` with:

```python
"""LangGraph nodes, state schema, checkpointer, and graph assembly.

Deliberately no re-exports here (ADR-030's fix, applied to this package
too): importing any submodule of this package always executes this file
first, and eagerly importing build.py/checkpointer.py would force
langgraph as a transitive dependency onto submodules that don't need it
(cost_aggregator.py, state.py's pure-dataclass pieces). Nothing in this
codebase imports from the package root -- confirmed via grep before this
file was stripped.
"""
```

- [ ] **Step 3: Confirm nothing broke**

Run: `python3 -m py_compile services/orchestrator/graph/__init__.py services/orchestrator/graph/build.py services/orchestrator/graph/checkpointer.py services/orchestrator/graph/state.py services/orchestrator/graph/nodes.py`
Expected: exits 0, no output.

- [ ] **Step 4: Commit**

```bash
git add services/orchestrator/graph/__init__.py
git commit -m "fix(orchestrator): strip dead eager imports from graph/__init__.py

Matches ADR-030's fix for agents/__init__.py -- nothing imports from the
package root, but the eager re-exports forced langgraph onto every
submodule import, including langgraph-free ones this phase adds."
```

---

### Task 2: Let `new_project_state()` seed `cost_usd_weekly` directly

Every existing test that exercises the cost gate seeds `cost_usd_session=...` because that's what the old gate read. The new gate reads `cost_usd_weekly`, so `new_project_state()` needs a matching constructor parameter before any gate test can be rewritten.

**Files:**
- Modify: `services/orchestrator/graph/state.py`
- Test: `services/orchestrator/graph/tests/unit/test_state.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `new_project_state(project_id, ..., cost_usd_weekly: float = 0.0, ...) -> ProjectState` — every later task that seeds a project's global cost for a test uses this parameter.

- [ ] **Step 1: Write the failing test**

Add to `services/orchestrator/graph/tests/unit/test_state.py`:

```python
def test_new_project_state_seeds_cost_usd_weekly():
    state = new_project_state("proj-1", cost_usd_weekly=12.5)

    assert state["cost_usd_weekly"] == 12.5
```

(Check the existing import at the top of that file already covers `new_project_state`; if not, add `from services.orchestrator.graph.state import new_project_state`.)

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest services/orchestrator/graph/tests/unit/test_state.py::test_new_project_state_seeds_cost_usd_weekly -v`
Expected: FAIL with `TypeError: new_project_state() got an unexpected keyword argument 'cost_usd_weekly'`

- [ ] **Step 3: Add the parameter**

In `services/orchestrator/graph/state.py`, change the `new_project_state` signature and body:

```python
def new_project_state(
    project_id: str,
    mode: Mode = "greenfield",
    stack: Stack = "python",
    coverage_threshold: int = 80,
    pr_mode: PrMode = "manual",
    coverage_gate_mode: CoverageGateMode = "hard",
    cost_usd_session: float = 0.0,
    cost_usd_weekly: float = 0.0,
    initial_message: str | None = None,
) -> ProjectState:
```

Add a matching docstring `Args:` entry right after `cost_usd_session`'s:

```python
        cost_usd_weekly: Starting weekly cost in USD. Non-zero values are
            only used to drive tests of the global cost-threshold gate
            (TODO.md Phase 18) -- a real deployment always starts this at
            0.0 and lets `_with_cost` accumulate it.
```

And change the constructor call at the bottom from `cost_usd_weekly=0.0,` to `cost_usd_weekly=cost_usd_weekly,`.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest services/orchestrator/graph/tests/unit/test_state.py -v`
Expected: PASS (all tests in the file, including the new one).

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/graph/state.py services/orchestrator/graph/tests/unit/test_state.py
git commit -m "feat(orchestrator): let new_project_state seed cost_usd_weekly

Needed so tests can drive the upcoming global cost-threshold gate
(TODO.md Phase 18), which reads cost_usd_weekly instead of
cost_usd_session."
```

---

### Task 3: `cost_aggregator.py` — sum other projects' latest checkpointed cost

`checkpointer.list(None)` returns every historical checkpoint *version* for every thread, not just the latest one per thread (`service.py`'s `list_projects()` already has to de-dupe thread_ids into a set for exactly this reason). Naively summing `cost_usd_weekly` across every row would count one project's spend many times over. This task adds a small, dependency-light helper that gets this right, with a test that specifically plants multiple historical checkpoints for one project to catch the double-counting bug if it creeps back in.

**Files:**
- Create: `services/orchestrator/graph/cost_aggregator.py`
- Test: `services/orchestrator/graph/tests/unit/test_cost_aggregator.py`

**Interfaces:**
- Consumes: a duck-typed checkpointer object with `.list(config)` (returns an iterable of objects with `.config` and `.checkpoint` attributes, `.config["configurable"]["thread_id"]` a string, `.checkpoint["channel_values"]` a dict) and `.get_tuple(config)` (returns one such object, or `None`, for the *latest* checkpoint of the thread named in `config["configurable"]["thread_id"]`) — this is `langgraph.checkpoint.base.BaseCheckpointSaver`'s real interface, matched by duck typing rather than importing the class, so this module stays importable without `langgraph` installed.
- Produces: `other_projects_cost_usd_weekly(checkpointer: Any, exclude_project_id: str) -> float`, used by `node_cost_check` (Task 4).

- [ ] **Step 1: Write the failing tests**

Create `services/orchestrator/graph/tests/unit/test_cost_aggregator.py`:

```python
"""Unit tests for other_projects_cost_usd_weekly (TODO.md Phase 18).

Uses a lightweight fake checkpointer (duck-typed against
langgraph.checkpoint.base.BaseCheckpointSaver's .list()/.get_tuple()
surface) rather than a real SqliteSaver, so this suite has no langgraph
dependency at all and runs in any environment.
"""

from dataclasses import dataclass, field

from services.orchestrator.graph.cost_aggregator import other_projects_cost_usd_weekly


@dataclass
class _FakeCheckpointTuple:
    thread_id: str
    cost_usd_weekly: float

    @property
    def config(self) -> dict:
        return {"configurable": {"thread_id": self.thread_id}}

    @property
    def checkpoint(self) -> dict:
        return {"channel_values": {"cost_usd_weekly": self.cost_usd_weekly}}


@dataclass
class _FakeCheckpointer:
    """Simulates multiple historical checkpoint versions per thread_id --
    .list() returns every version ever written (oldest first, matching
    SqliteSaver's real behavior), .get_tuple() returns only the latest."""

    versions_by_thread: dict[str, list[float]] = field(default_factory=dict)

    def list(self, config):
        for thread_id, versions in self.versions_by_thread.items():
            for cost in versions:
                yield _FakeCheckpointTuple(thread_id, cost)

    def get_tuple(self, config):
        thread_id = config["configurable"]["thread_id"]
        versions = self.versions_by_thread.get(thread_id)
        if not versions:
            return None
        return _FakeCheckpointTuple(thread_id, versions[-1])


def test_other_projects_cost_usd_weekly_sums_latest_checkpoint_per_thread():
    checkpointer = _FakeCheckpointer(
        {
            "proj-a": [5.0, 5.0, 12.0],  # three historical versions -- only 12.0 should count
            "proj-b": [3.0],
        }
    )

    total = other_projects_cost_usd_weekly(checkpointer, exclude_project_id="proj-c")

    assert total == 15.0  # 12.0 (proj-a's latest) + 3.0 (proj-b's latest), NOT 5+5+12+3


def test_other_projects_cost_usd_weekly_excludes_the_given_project():
    checkpointer = _FakeCheckpointer({"proj-a": [10.0], "proj-b": [3.0]})

    total = other_projects_cost_usd_weekly(checkpointer, exclude_project_id="proj-a")

    assert total == 3.0


def test_other_projects_cost_usd_weekly_returns_zero_with_no_other_projects():
    checkpointer = _FakeCheckpointer({"proj-a": [10.0]})

    total = other_projects_cost_usd_weekly(checkpointer, exclude_project_id="proj-a")

    assert total == 0.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest services/orchestrator/graph/tests/unit/test_cost_aggregator.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'services.orchestrator.graph.cost_aggregator'`

- [ ] **Step 3: Write the implementation**

Create `services/orchestrator/graph/cost_aggregator.py`:

```python
"""Sums other projects' latest checkpointed weekly cost (TODO.md Phase 18).

The cost-threshold gate (ADR-009) is global, not per-project (TODO.md
Phase 18) -- node_cost_check needs every *other* project's most recent
cost_usd_weekly, added to the current project's own live value. Takes the
checkpointer as a duck-typed `Any` rather than importing
`langgraph.checkpoint.base.BaseCheckpointSaver` for the type hint, so this
module (and its unit tests) stay importable without langgraph installed --
every real caller (graph/build.py) already has langgraph as a hard
dependency anyway.
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)


def other_projects_cost_usd_weekly(checkpointer: Any, exclude_project_id: str) -> float:
    """Sums the latest checkpointed cost_usd_weekly across every project but one.

    `checkpointer.list(None)` returns every historical checkpoint *version*
    for every thread, not just the latest one per thread -- summing across
    all of them would count one project's spend as many times as it has
    checkpoints. This walks the distinct thread_ids once, then fetches only
    each thread's latest checkpoint via `get_tuple()`.

    Args:
        checkpointer: A BaseCheckpointSaver-shaped object (duck-typed, see
            module docstring): `.list(None)` yields objects with
            `.config["configurable"]["thread_id"]`, and `.get_tuple(config)`
            returns the latest such object (or None) for one thread_id.
        exclude_project_id: The project_id to omit from the sum -- callers
            pass the current project's own project_id and add its live
            `state["cost_usd_weekly"]` themselves, since that's fresher
            than whatever's already checkpointed for it.

    Returns:
        The sum of every other known project's latest `cost_usd_weekly`,
        defaulting an individual project's contribution to 0.0 if its
        latest checkpoint has no `cost_usd_weekly` channel value yet.
    """
    thread_ids = {
        checkpoint_tuple.config["configurable"]["thread_id"]
        for checkpoint_tuple in checkpointer.list(None)
    } - {exclude_project_id}

    total = 0.0
    for thread_id in thread_ids:
        latest = checkpointer.get_tuple({"configurable": {"thread_id": thread_id}})
        if latest is None:
            continue
        total += latest.checkpoint["channel_values"].get("cost_usd_weekly", 0.0)
    return total
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest services/orchestrator/graph/tests/unit/test_cost_aggregator.py -v`
Expected: PASS (3 passed). This suite has no `langgraph`/`langchain`/`fastapi` dependency (confirm Task 1 landed first, or the import chain still pulls in `langgraph` via `graph/__init__.py`) — run `python3 -c "import services.orchestrator.graph.cost_aggregator"` standalone too and confirm it doesn't raise `ModuleNotFoundError` for `langgraph` specifically.

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/graph/cost_aggregator.py services/orchestrator/graph/tests/unit/test_cost_aggregator.py
git commit -m "feat(orchestrator): add other_projects_cost_usd_weekly aggregator

Sums every other project's latest checkpointed cost_usd_weekly for the
upcoming global cost-threshold gate (TODO.md Phase 18), reading only each
thread's latest checkpoint to avoid double-counting historical versions."
```

---

### Task 4: Replace `node_cost_check`'s per-project stub gate with the global one

**Files:**
- Modify: `services/orchestrator/graph/nodes.py:95, 406-436`
- Test: `services/orchestrator/graph/tests/unit/test_nodes.py:20-41, 290-326`

**Interfaces:**
- Consumes: `other_projects_cost_usd_weekly(checkpointer, exclude_project_id) -> float` (Task 3).
- Produces: `node_cost_check(state: ProjectState, checkpointer: Any, threshold_usd: float) -> dict[str, Any]`, wired into the graph by Task 5's `functools.partial`.

- [ ] **Step 1: Write the failing tests**

In `services/orchestrator/graph/tests/unit/test_nodes.py`, replace the `node_cost_check as cost_check` import (currently on its own line, per the file's existing `from services.orchestrator.graph.nodes import node_cost_check as cost_check`) — keep the alias, no change needed there. Remove `COST_THRESHOLD_USD_STUB` from the multi-line import block starting at line 20 (it's being deleted from `nodes.py` in Step 3).

Replace the two existing tests (lines 290-312) with:

```python
class _FakeCheckpointer:
    def __init__(self, others: dict[str, float]):
        self._others = others

    def list(self, config):
        for thread_id in self._others:
            yield type(
                "T", (), {"config": {"configurable": {"thread_id": thread_id}}}
            )()

    def get_tuple(self, config):
        thread_id = config["configurable"]["thread_id"]
        if thread_id not in self._others:
            return None
        return type(
            "T",
            (),
            {"checkpoint": {"channel_values": {"cost_usd_weekly": self._others[thread_id]}}},
        )()


def test_node_cost_check_returns_empty_update_below_global_threshold():
    state = new_project_state("proj-1", cost_usd_weekly=10.0)
    checkpointer = _FakeCheckpointer({"proj-2": 20.0})  # 10 + 20 = 30, under 50

    assert cost_check(state, checkpointer=checkpointer, threshold_usd=50.0) == {}


def test_node_cost_check_arms_gate_when_global_total_crosses_threshold():
    state = new_project_state("proj-1", cost_usd_weekly=10.0)
    checkpointer = _FakeCheckpointer({"proj-2": 45.0})  # 10 + 45 = 55, over 50

    update = cost_check(state, checkpointer=checkpointer, threshold_usd=50.0)

    assert update["phase"] == "awaiting_cost_approval"
    assert update["pending_approval"] == {
        "kind": "cost_alert",
        "thread_id": "proj-1",
        "project_id": "proj-1",
        "cost_usd_weekly_global": 55.0,
        "threshold_usd": 50.0,
        "summary": "Crossed $55.00 of $50.00 global weekly cost limit -- continue?",
    }


def test_node_cost_check_trips_purely_from_other_projects_even_with_zero_own_cost():
    state = new_project_state("proj-1", cost_usd_weekly=0.0)
    checkpointer = _FakeCheckpointer({"proj-2": 60.0})

    update = cost_check(state, checkpointer=checkpointer, threshold_usd=50.0)

    assert update["phase"] == "awaiting_cost_approval"
```

(The `type("T", (), {...})()` construction is a minimal one-off object with the two attributes `other_projects_cost_usd_weekly` actually reads — no need for a full checkpoint-tuple-shaped dataclass here since these tests only exercise `node_cost_check`, not the aggregator itself, which already has its own thorough tests in Task 3.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest services/orchestrator/graph/tests/unit/test_nodes.py -k cost_check -v`
Expected: FAIL — `cost_check() got an unexpected keyword argument 'checkpointer'` (or similar `TypeError`), since `node_cost_check` doesn't accept those parameters yet.

- [ ] **Step 3: Update `node_cost_check`**

In `services/orchestrator/graph/nodes.py`:

1. Delete line 95 (`COST_THRESHOLD_USD_STUB = 50.0`).
2. Add the import near the top, alongside the other `services.orchestrator.graph` imports:

```python
from services.orchestrator.graph.cost_aggregator import other_projects_cost_usd_weekly
```

3. Replace `node_cost_check` and `route_after_cost_check` (currently lines 406-436) with:

```python
def node_cost_check(
    state: ProjectState, checkpointer: Any, threshold_usd: float
) -> dict[str, Any]:
    """Trips the cost-threshold gate if the *global* weekly total is at or above threshold_usd.

    The gate is global, not per-project (TODO.md Phase 18, ADR-009): sums
    every other known project's latest checkpointed cost_usd_weekly
    (other_projects_cost_usd_weekly) plus this project's own live
    cost_usd_weekly from state. Returns an empty update (no gate) unless
    that combined total has reached threshold_usd, in which case it commits
    the pending approval payload node_cost_wait will interrupt on next.
    """
    global_total = state["cost_usd_weekly"] + other_projects_cost_usd_weekly(
        checkpointer, exclude_project_id=state["project_id"]
    )
    if global_total < threshold_usd:
        return {}
    logger.info(
        "project %s: global cost threshold reached (%.2f >= %.2f)",
        state["project_id"],
        global_total,
        threshold_usd,
    )
    return {
        "phase": "awaiting_cost_approval",
        "pending_approval": {
            "kind": "cost_alert",
            "thread_id": state["project_id"],
            "project_id": state["project_id"],
            "cost_usd_weekly_global": global_total,
            "threshold_usd": threshold_usd,
            "summary": (
                f"Crossed ${global_total:.2f} of "
                f"${threshold_usd:.2f} global weekly cost limit -- continue?"
            ),
        },
    }


def route_after_cost_check(state: ProjectState) -> str:
    """Routes to the wait node only if node_cost_check just tripped the gate."""
    if state["phase"] == "awaiting_cost_approval":
        return "cost_wait"
    return "testing"
```

(`route_after_cost_check` is unchanged — reproduced above only so the two functions read together correctly; no edit needed to it beyond leaving it exactly as-is.)

4. Confirm `Any` is already imported in `nodes.py` (it is — `from typing import Any` is already present per the existing module).

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest services/orchestrator/graph/tests/unit/test_nodes.py -v`
Expected: PASS — all tests in the file, including the three rewritten cost-check tests. If any *other* existing test in this file also references `COST_THRESHOLD_USD_STUB`, remove that reference too (the Step 1 grep in Task-level review should have caught all of them; re-run `grep -n COST_THRESHOLD_USD_STUB services/orchestrator/graph/tests/unit/test_nodes.py` and confirm no output).

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/graph/nodes.py services/orchestrator/graph/tests/unit/test_nodes.py
git commit -m "feat(orchestrator): node_cost_check gates on global weekly cost

Replaces the per-project cost_usd_session-vs-hardcoded-stub check with a
global cost_usd_weekly total across every active project (TODO.md Phase
18) -- the cost gate was always meant to be global (ADR-009) but the
original Phase 5 stub only ever compared one project's own session cost."
```

---

### Task 5: Thread `global_cost_threshold_usd` through `build_project_graph`

**Files:**
- Modify: `services/orchestrator/graph/build.py`
- Test: `services/orchestrator/graph/tests/unit/test_graph_end_to_end.py`

**Interfaces:**
- Consumes: `node_cost_check(state, checkpointer, threshold_usd)` (Task 4).
- Produces: `build_project_graph(checkpointer, router, executor_client, memory_conn, projects_root, global_cost_threshold_usd: float = DEFAULT_GLOBAL_COST_THRESHOLD_USD) -> CompiledStateGraph`, and the module-level constant `DEFAULT_GLOBAL_COST_THRESHOLD_USD`, consumed by Task 7's `OrchestratorService`.

- [ ] **Step 1: Write the failing test**

In `services/orchestrator/graph/tests/unit/test_graph_end_to_end.py`:

1. Change the import on line 29 from:
```python
from services.orchestrator.graph.nodes import COST_THRESHOLD_USD_STUB
```
to:
```python
from services.orchestrator.graph.build import DEFAULT_GLOBAL_COST_THRESHOLD_USD
```

2. In the four places that seed `cost_usd_session=COST_THRESHOLD_USD_STUB` (lines 136, 155, 174, 239), change the keyword to `cost_usd_weekly=DEFAULT_GLOBAL_COST_THRESHOLD_USD`. For example, line 136 becomes:
```python
        new_project_state("proj-over-budget", cost_usd_weekly=DEFAULT_GLOBAL_COST_THRESHOLD_USD),
```
(and identically for the other three call sites, substituting each test's own project_id).

3. Add a new test proving the threshold is actually configurable end to end:

```python
def test_cost_interrupt_fires_from_a_lower_configured_global_threshold(
    tmp_path, memory_conn, projects_root
):
    checkpointer = build_sqlite_checkpointer(tmp_path / "foundry_memory.db")
    graph = build_project_graph(
        checkpointer,
        fast_track_router(),
        _UNUSED_EXECUTOR_CLIENT,
        memory_conn,
        projects_root,
        global_cost_threshold_usd=1.0,
    )
    config = _config("proj-tiny-budget")

    graph.invoke(new_project_state("proj-tiny-budget", cost_usd_weekly=2.0), config=config)
    graph.invoke(Command(resume="approve"), config=config)

    snapshot = graph.get_state(config)
    assert snapshot.values["phase"] == "awaiting_cost_approval"
    assert snapshot.values["pending_approval"]["threshold_usd"] == 1.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest services/orchestrator/graph/tests/unit/test_graph_end_to_end.py -v`
Expected: FAIL — `ImportError: cannot import name 'DEFAULT_GLOBAL_COST_THRESHOLD_USD' from 'services.orchestrator.graph.build'`

- [ ] **Step 3: Wire the parameter into `build_project_graph`**

In `services/orchestrator/graph/build.py`:

1. Add near the top, after the imports:

```python
DEFAULT_GLOBAL_COST_THRESHOLD_USD = 50.0
```

2. Change the function signature:

```python
def build_project_graph(
    checkpointer: BaseCheckpointSaver,
    router: ModelRouter,
    executor_client: ExecutorClient,
    memory_conn: sqlite3.Connection,
    projects_root: Path,
    global_cost_threshold_usd: float = DEFAULT_GLOBAL_COST_THRESHOLD_USD,
) -> CompiledStateGraph:
```

Add a matching `Args:` docstring line after `projects_root`'s:

```python
        global_cost_threshold_usd: The global weekly USD threshold
            node_cost_check gates on (TODO.md Phase 18) -- summed across
            every active project's checkpointed cost_usd_weekly, not just
            this one. Defaults to DEFAULT_GLOBAL_COST_THRESHOLD_USD; a real
            deployment passes OrchestratorConfig.global_cost_threshold_usd.
```

3. Change the `cost_check` node registration from:
```python
    builder.add_node("cost_check", node_cost_check)
```
to:
```python
    builder.add_node(
        "cost_check",
        functools.partial(
            node_cost_check,
            checkpointer=checkpointer,
            threshold_usd=global_cost_threshold_usd,
        ),
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest services/orchestrator/graph/tests/unit/test_graph_end_to_end.py -v`
Expected: PASS (all tests, including the new configurable-threshold one).

Also run the full graph unit suite to catch any other stale reference:
Run: `pytest services/orchestrator/graph/tests/unit -v`
Expected: PASS. (`test_integrator.py`'s `build_project_graph(...)` call has no cost-related seeding and doesn't pass the new parameter, so it keeps the `50.0` default and is unaffected.)

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/graph/build.py services/orchestrator/graph/tests/unit/test_graph_end_to_end.py
git commit -m "feat(orchestrator): thread global_cost_threshold_usd through build_project_graph

Wires node_cost_check's new checkpointer/threshold_usd dependencies via
the same functools.partial pattern every other node dependency already
uses (TODO.md Phase 18)."
```

---

### Task 6: `OrchestratorConfig.global_cost_threshold_usd`

**Files:**
- Modify: `services/orchestrator/config.py`
- Test: `services/orchestrator/tests/unit/test_config.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `OrchestratorConfig.global_cost_threshold_usd: float`, read from `GLOBAL_COST_THRESHOLD_USD` in `.from_env()`, consumed by Task 7's `main.py`/`registry/cli.py` changes.

- [ ] **Step 1: Write the failing tests**

Add to `services/orchestrator/tests/unit/test_config.py`:

```python
def test_from_env_defaults_global_cost_threshold_usd(monkeypatch):
    monkeypatch.delenv("GLOBAL_COST_THRESHOLD_USD", raising=False)

    config = OrchestratorConfig.from_env()

    assert config.global_cost_threshold_usd == 50.0


def test_from_env_reads_global_cost_threshold_usd_override(monkeypatch):
    monkeypatch.setenv("GLOBAL_COST_THRESHOLD_USD", "125.5")

    config = OrchestratorConfig.from_env()

    assert config.global_cost_threshold_usd == 125.5
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest services/orchestrator/tests/unit/test_config.py -v`
Expected: FAIL with `pydantic.ValidationError` (missing required field `global_cost_threshold_usd`) or `AttributeError`.

- [ ] **Step 3: Add the field**

In `services/orchestrator/config.py`:

```python
DEFAULT_MEMORY_DB_PATH = Path("/data/memory/foundry_memory.db")
DEFAULT_EXECUTOR_URL = "http://executor:8000"
DEFAULT_PROJECTS_ROOT = Path("/data/projects")
DEFAULT_GLOBAL_COST_THRESHOLD_USD = 50.0


class OrchestratorConfig(BaseModel):
    """Runtime configuration for the Orchestrator service."""

    memory_db_path: Path
    executor_url: str
    projects_root: Path
    global_cost_threshold_usd: float
```

And in `from_env()`, add before the `return cls(...)`:

```python
        raw_threshold = os.environ.get("GLOBAL_COST_THRESHOLD_USD")
        global_cost_threshold_usd = (
            float(raw_threshold) if raw_threshold else DEFAULT_GLOBAL_COST_THRESHOLD_USD
        )
```

and add `global_cost_threshold_usd=global_cost_threshold_usd,` to the `return cls(...)` call. Update the method's docstring `Returns:` line to mention `global_cost_threshold_usd` defaults to `50.0` when `GLOBAL_COST_THRESHOLD_USD` is unset, alongside the existing `memory_db_path`/`executor_url` defaults description.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest services/orchestrator/tests/unit/test_config.py -v`
Expected: PASS (all tests in the file).

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/config.py services/orchestrator/tests/unit/test_config.py
git commit -m "feat(orchestrator): add configurable GLOBAL_COST_THRESHOLD_USD

Delivers ADR-009's 'globally configurable' cost threshold for real --
previously only ever a hardcoded stub in graph/nodes.py."
```

---

### Task 7: Wire the config value through `OrchestratorService`, `main.py`, `registry/cli.py`

**Files:**
- Modify: `services/orchestrator/service.py`
- Modify: `services/orchestrator/main.py`
- Modify: `services/orchestrator/registry/cli.py`

**Interfaces:**
- Consumes: `build_project_graph(..., global_cost_threshold_usd=...)` (Task 5), `OrchestratorConfig.global_cost_threshold_usd` (Task 6).
- Produces: `OrchestratorService.__init__(..., global_cost_threshold_usd: float = DEFAULT_GLOBAL_COST_THRESHOLD_USD)`.

- [ ] **Step 1: Update `OrchestratorService.__init__`**

In `services/orchestrator/service.py`, add the import:

```python
from services.orchestrator.graph.build import DEFAULT_GLOBAL_COST_THRESHOLD_USD, build_project_graph
```

(replacing the existing `from services.orchestrator.graph.build import build_project_graph` line). Change `__init__`:

```python
    def __init__(
        self,
        checkpointer: BaseCheckpointSaver,
        router: ModelRouter,
        executor_client: ExecutorClient,
        memory_conn: sqlite3.Connection,
        projects_root: Path,
        global_cost_threshold_usd: float = DEFAULT_GLOBAL_COST_THRESHOLD_USD,
    ) -> None:
```

Add a matching `Args:` line after `projects_root`'s:

```python
            global_cost_threshold_usd: The global weekly USD cost-gate
                threshold (TODO.md Phase 18); defaults to
                DEFAULT_GLOBAL_COST_THRESHOLD_USD. A real deployment passes
                OrchestratorConfig.global_cost_threshold_usd.
```

And change the body's graph construction:

```python
        self._graph = build_project_graph(
            checkpointer, router, executor_client, memory_conn, projects_root,
            global_cost_threshold_usd,
        )
```

- [ ] **Step 2: Update `main.py`**

In `services/orchestrator/main.py`, change:

```python
    service = OrchestratorService(
        checkpointer, _build_router(), executor_client, memory_conn, config.projects_root
    )
```

to:

```python
    service = OrchestratorService(
        checkpointer,
        _build_router(),
        executor_client,
        memory_conn,
        config.projects_root,
        config.global_cost_threshold_usd,
    )
```

- [ ] **Step 3: Update `registry/cli.py`**

In `services/orchestrator/registry/cli.py`'s `_start_intake`, change:

```python
    OrchestratorService(
        checkpointer, router, executor_client, memory_conn, config.projects_root
    ).handle_webhook(project_id, description)
```

to:

```python
    OrchestratorService(
        checkpointer,
        router,
        executor_client,
        memory_conn,
        config.projects_root,
        config.global_cost_threshold_usd,
    ).handle_webhook(project_id, description)
```

- [ ] **Step 4: Verify nothing broke**

Run: `python3 -m py_compile services/orchestrator/service.py services/orchestrator/main.py services/orchestrator/registry/cli.py`
Expected: exits 0.

Run: `pytest services/orchestrator/tests/unit/test_service.py services/orchestrator/registry -v`
Expected: PASS (the existing `service` fixture in `test_service.py` doesn't pass a 6th positional arg, so it uses the new default and needs no changes; same for `registry/cli.py`'s own tests, if any construct `OrchestratorService` directly — check with `grep -rn "OrchestratorService(" services/orchestrator/registry` first and update any such call site the same way as Step 3 if found).

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/service.py services/orchestrator/main.py services/orchestrator/registry/cli.py
git commit -m "feat(orchestrator): thread global_cost_threshold_usd into OrchestratorService

Both real entrypoints (main.py's FastAPI server, registry/cli.py's
new-project intake) now pass OrchestratorConfig.global_cost_threshold_usd
through instead of relying on the graph-layer default."
```

---

### Task 8: `_ThrottledChatModel` — serialize Ollama-backed calls through one semaphore

**Files:**
- Modify: `services/orchestrator/model_router/router.py`

**Interfaces:**
- Consumes: nothing new (wraps `ChatOllama` instances `ModelRouter.__init__` already constructs).
- Produces: `_ThrottledChatModel` (module-private class, imported by name in tests per this repo's existing precedent of testing underscore-prefixed helpers, e.g. `agents/coder.py`'s `_TaskList`). `ModelRouter.resolve(role)` returns a `_ThrottledChatModel`-wrapped model for any role resolving to an `ollama`-type provider, and an unwrapped `ChatAnthropic` for any `anthropic`-type provider — same as before.

- [ ] **Step 1: Write the failing tests**

Add to `services/orchestrator/model_router/tests/unit/test_router.py`:

```python
def test_router_wraps_ollama_model_in_throttle_but_leaves_anthropic_unwrapped():
    from services.orchestrator.model_router.router import _ThrottledChatModel

    config = _config(
        {
            "roles": {
                "local_role": {"requires": [], "prefer": ["local"]},
                "claude_role": {"requires": [], "prefer": ["claude_sonnet"]},
            },
            "providers": {
                "local": {
                    "type": "ollama",
                    "model": "qwen3-coder",
                    "base_url": "http://ollama:11434",
                    "capabilities": [],
                    "cost_per_1m_input_tokens": 0,
                    "cost_per_1m_output_tokens": 0,
                },
                "claude_sonnet": {
                    "type": "anthropic",
                    "model": "claude-sonnet-5",
                    "capabilities": [],
                    "cost_per_1m_input_tokens": 3.0,
                    "cost_per_1m_output_tokens": 15.0,
                },
            },
        }
    )
    router = ModelRouter(config, ollama_enabled=True, anthropic_api_key="key")

    assert isinstance(router.resolve("local_role"), _ThrottledChatModel)
    assert not isinstance(router.resolve("claude_role"), _ThrottledChatModel)


def test_throttled_chat_model_serializes_concurrent_ainvoke_calls():
    """Two 'simultaneous' calls through the same throttle never overlap --
    proving the shared-GPU serialization constraint (TODO.md Phase 18).
    Uses threading (not asyncio) concurrency deliberately: every real
    LLM-backed graph node bridges to its one async call via its own
    asyncio.run(), each in its own OS thread when two projects run
    concurrently (ADR-019), so this test reproduces that shape directly.
    """
    import asyncio
    import threading
    import time
    from concurrent.futures import ThreadPoolExecutor

    from langchain_core.messages import AIMessage

    from services.orchestrator.model_router.router import _ThrottledChatModel

    active = 0
    max_active = 0
    lock = threading.Lock()

    class _SlowFakeModel:
        async def ainvoke(self, *args, **kwargs):
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.05)
            with lock:
                active -= 1
            return AIMessage(content="done")

    throttled = _ThrottledChatModel(_SlowFakeModel(), threading.Semaphore(1))

    def call():
        asyncio.run(throttled.ainvoke("hi"))

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(call), pool.submit(call)]
        for future in futures:
            future.result()

    assert max_active == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest services/orchestrator/model_router/tests/unit/test_router.py -k throttle -v`
Expected: FAIL with `ImportError: cannot import name '_ThrottledChatModel' from 'services.orchestrator.model_router.router'`

- [ ] **Step 3: Implement `_ThrottledChatModel` and wire it into `ModelRouter.__init__`**

In `services/orchestrator/model_router/router.py`, add `import threading` to the imports, and add this class after the module-level constants (`OLLAMA_ENABLED_ENV_VAR`/`PROVIDER_MAX_RETRIES`), before `class ModelRouter`:

```python
class _ThrottledChatModel:
    """Wraps a BaseChatModel so at most one call is ever in flight at a time.

    Local inference shares one physical GPU (TODO.md Phase 18), so
    ModelRouter wraps every Ollama-backed model in this class before
    storing it; ChatAnthropic entries are stored unwrapped, so
    Claude-API-bound work runs in parallel without this constraint.

    A threading.Semaphore, not asyncio.Semaphore, is required: every
    LLM-backed graph node bridges to its one async call via its own
    asyncio.run() (ADR-019), so when two projects run concurrently each
    gets its own OS thread and its own event loop -- an asyncio.Semaphore
    scoped to one loop can't coordinate across them.

    Covers exactly the four methods anything in this codebase calls on a
    resolved model (agents/llm.py's invoke_structured, agents/coder.py's
    tool-calling loop): .ainvoke(), .astream(), .bind_tools(),
    .with_structured_output(). The latter two return a new
    _ThrottledChatModel wrapping whatever Runnable the real model's
    equivalent method returns, so throttling survives the call chains those
    two callers actually use. Deliberately duck-typed rather than a
    BaseChatModel subclass -- LangChain's BaseChatModel has several
    abstract methods (_generate/_agenerate/_stream/_astream) this wrapper
    has no need to implement, since nothing in this codebase calls anything
    beyond these four entry points on a router.resolve() result (confirmed
    by a repo-wide grep during Phase 18 planning).
    """

    def __init__(self, model: Any, semaphore: threading.Semaphore) -> None:
        self._model = model
        self._semaphore = semaphore

    async def ainvoke(self, *args: Any, **kwargs: Any) -> Any:
        with self._semaphore:
            return await self._model.ainvoke(*args, **kwargs)

    async def astream(self, *args: Any, **kwargs: Any) -> Any:
        with self._semaphore:
            async for chunk in self._model.astream(*args, **kwargs):
                yield chunk

    def bind_tools(self, *args: Any, **kwargs: Any) -> "_ThrottledChatModel":
        return _ThrottledChatModel(self._model.bind_tools(*args, **kwargs), self._semaphore)

    def with_structured_output(self, *args: Any, **kwargs: Any) -> "_ThrottledChatModel":
        return _ThrottledChatModel(
            self._model.with_structured_output(*args, **kwargs), self._semaphore
        )
```

Add `from typing import Any` to the imports if not already present (check first — `router.py` currently has no `typing` import).

Then update `ModelRouter.__init__`. Add one line right after `self._models: dict[str, BaseChatModel] = {}` is set:

```python
        self._local_semaphore = threading.Semaphore(1)
```

And change the `ollama` branch of the provider-construction loop from:

```python
            if provider_config.type == "ollama":
                if not ollama_enabled:
                    continue
                self._models[provider_name] = ChatOllama(
                    model=provider_config.model,
                    base_url=provider_config.base_url,
                    max_retries=PROVIDER_MAX_RETRIES,
                )
```

to:

```python
            if provider_config.type == "ollama":
                if not ollama_enabled:
                    continue
                raw_model = ChatOllama(
                    model=provider_config.model,
                    base_url=provider_config.base_url,
                    max_retries=PROVIDER_MAX_RETRIES,
                )
                self._models[provider_name] = _ThrottledChatModel(raw_model, self._local_semaphore)
```

Update the class docstring of `ModelRouter` (currently ending "...Every constructed model has retries enabled...") to add one sentence: "Every Ollama-backed model is additionally wrapped in `_ThrottledChatModel` so local inference never runs more than one call at a time (TODO.md Phase 18) — one physical GPU serves every Ollama-backed role in a deployment, regardless of how many are declared in `models.yaml`."

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest services/orchestrator/model_router/tests/unit/test_router.py -v`
Expected: PASS (every existing test plus the two new ones — confirm none of the existing `isinstance(model, ChatAnthropic)`/`ChatOllama.__init__` spy assertions broke; they shouldn't, since `_ThrottledChatModel` only wraps the *stored* reference, and the spy on `ChatOllama.__init__` still fires during real construction either way).

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/model_router/router.py services/orchestrator/model_router/tests/unit/test_router.py
git commit -m "feat(model_router): serialize Ollama-backed calls through one semaphore

Every Ollama-backed model ModelRouter constructs is now wrapped in
_ThrottledChatModel, sharing one threading.Semaphore(1) per router
instance (TODO.md Phase 18: 'GPU is shared'). Claude-API-bound work is
unaffected and continues to run in parallel. No agent module (planner/
coder/reviewer/frontend_reviewer/memory_curator) needed to change --
the throttle is fully contained behind ModelRouter.resolve()'s existing
BaseChatModel-shaped return value."
```

---

### Task 9: Thread-isolation test — two interleaved projects, no state bleed

**Files:**
- Create: `services/orchestrator/graph/tests/unit/test_concurrency.py`

**Interfaces:**
- Consumes: `build_project_graph` (Task 5's signature), `new_project_state` (Task 2's signature), `FakeRouter`/`structured`/`CODER_DONE`/`REVIEWER_APPROVE` from `planner_fakes.py`, `CoderTask`/`_TaskList` from `agents/coder.py`, `CritiqueResult`/`DraftPlan`/`InterviewTurn` from `agents/planner.py`.
- Produces: nothing consumed elsewhere — this is the Definition-of-Done proof for "the interleaved two-project test shows no state bleed."

- [ ] **Step 1: Write the test**

Create `services/orchestrator/graph/tests/unit/test_concurrency.py`:

```python
"""Phase 18 -- proves LangGraph's per-thread_id checkpointing (ADR-006)
doesn't bleed ProjectState between concurrently active projects.

Drives two fixture projects through the same compiled graph, interleaved,
using one shared FakeRouter whose scripted responses are ordered to match
the exact invoke() sequence below -- this test controls that sequence
directly (no true OS-thread concurrency needed), so it proves the property
actually under test here: LangGraph's thread_id-keyed state isolation, not
anything about Python-level threading (see model_router's own
test_throttled_chat_model_serializes_concurrent_ainvoke_calls for that).
"""

from langgraph.types import Command

from services.orchestrator.agents.coder import CoderTask, _TaskList
from services.orchestrator.agents.planner import CritiqueResult, DraftPlan, InterviewTurn
from services.orchestrator.executor_client import CommandResult, TestResult
from services.orchestrator.graph.build import build_project_graph
from services.orchestrator.graph.checkpointer import build_sqlite_checkpointer
from services.orchestrator.graph.state import new_project_state
from services.orchestrator.graph.tests.unit.planner_fakes import (
    CODER_DONE,
    REVIEWER_APPROVE,
    FakeRouter,
    structured,
)


class _StubExecutorClient:
    """Reports a clean passing test run and successful git commands --
    same shape as test_graph_end_to_end.py's copy (deliberately not shared,
    per planner_fakes.py's own precedent for not coupling test fixtures
    across files)."""

    async def run_tests(self, project_id: str, framework: str = "pytest", stack: str = "python"):
        return TestResult(passed=1, failed=0, coverage_pct=100.0, report="1 passed")

    async def run_command(self, project_id: str, command, timeout=None, stack: str = "python"):
        return CommandResult(stdout="", stderr="", exit_code=0)


def _config(project_id: str) -> dict:
    return {"configurable": {"thread_id": project_id}}


def _scripted_pass(plan_summary: str, task_description: str) -> list:
    """One project's full Planner->Coder->Reviewer scripted response
    sequence, distinguishable from another project's by plan_summary/
    task_description -- matches fast_track_router()'s per-round shape in
    planner_fakes.py, but with per-project content instead of the same
    fixed EMPTY_DRAFT/ONE_TASK every round."""
    return [
        structured(InterviewTurn(status="ready")),
        structured(DraftPlan(summary=plan_summary)),
        structured(CritiqueResult()),
        structured(_TaskList(tasks=[CoderTask(description=task_description)])),
        CODER_DONE,
        REVIEWER_APPROVE,
    ]


def test_two_interleaved_projects_do_not_bleed_plan_or_messages(
    tmp_path, memory_conn, projects_root
):
    checkpointer = build_sqlite_checkpointer(tmp_path / "foundry_memory.db")
    router = FakeRouter(
        _scripted_pass("Alpha-only plan", "Alpha-only task")
        + _scripted_pass("Beta-only plan", "Beta-only task")
    )
    graph = build_project_graph(
        checkpointer, router, _StubExecutorClient(), memory_conn, projects_root
    )
    config_a = _config("proj-alpha")
    config_b = _config("proj-beta")

    # Interleave: drive Alpha to its plan-approval interrupt, then Beta to
    # its own -- consuming the shared router's scripted responses in the
    # exact order this test controls.
    graph.invoke(
        new_project_state("proj-alpha", initial_message="build alpha"), config=config_a
    )
    graph.invoke(
        new_project_state("proj-beta", initial_message="build beta"), config=config_b
    )

    snapshot_a = graph.get_state(config_a)
    snapshot_b = graph.get_state(config_b)
    assert snapshot_a.values["phase"] == "awaiting_plan_approval"
    assert snapshot_b.values["phase"] == "awaiting_plan_approval"
    assert snapshot_a.values["plan"]["summary"] == "Alpha-only plan"
    assert snapshot_b.values["plan"]["summary"] == "Beta-only plan"
    assert snapshot_a.values["messages"][0].content == "build alpha"
    assert snapshot_b.values["messages"][0].content == "build beta"
    assert snapshot_a.values["project_id"] == "proj-alpha"
    assert snapshot_b.values["project_id"] == "proj-beta"

    # Interleave the resumes too: approve Alpha, then Beta.
    graph.invoke(Command(resume="approve"), config=config_a)
    graph.invoke(Command(resume="approve"), config=config_b)

    final_a = graph.get_state(config_a)
    final_b = graph.get_state(config_b)
    assert final_a.values["phase"] == "awaiting_merge_approval"
    assert final_b.values["phase"] == "awaiting_merge_approval"
    assert final_a.values["plan"]["summary"] == "Alpha-only plan"
    assert final_b.values["plan"]["summary"] == "Beta-only plan"
    assert final_a.values["project_id"] == "proj-alpha"
    assert final_b.values["project_id"] == "proj-beta"
```

- [ ] **Step 2: Run the test to verify it fails for the right reason first**

Run: `pytest services/orchestrator/graph/tests/unit/test_concurrency.py -v`
Expected in this sandbox (no `langgraph` installed): FAIL/ERROR at collection with `ModuleNotFoundError: No module named 'langgraph'` — this is the same pre-existing gap every graph-layer test in this repo has carried since Phase 5 (see each prior phase's TODO.md verification note). This is expected, not a real failure — proceed to Step 3 to hand-trace, and re-run for real once `langgraph`/`langchain-anthropic`/`langchain-ollama` are installed.

- [ ] **Step 3: Hand-trace against the real node implementations**

Confirm each assumption against the actual code (do this by reading, since the test can't run here):
- `node_intake` always sets `phase="planning"` regardless of input — both projects start there. ✓ (`graph/nodes.py`)
- `node_planner_ask`/`planner_draft`/`planner_critique`/`plan_wait` consume exactly 3 scripted responses (`InterviewTurn(status="ready")`, `DraftPlan`, `CritiqueResult`) before interrupting at `plan_wait` — matches `fast_track_router()`'s own established shape, reused here with different content. ✓
- After `Command(resume="approve")`, `route_after_plan_wait` -> `memory_curator` -> `route_after_memory_curator` -> `task_decomposition` -> `coder` -> `reviewer` -> `frontend_review` -> `cost_check` -> `testing` -> `memory_curator` -> `merge_wait`, consuming exactly the remaining 3 scripted responses (`_TaskList`, `CODER_DONE`, `ReviewResult(verdict="approve")`) — matches the existing, already-passing `test_project_below_cost_threshold_reaches_plan_approval_then_merge_gate` test's proven path using the same `_StubExecutorClient` shape (no `list_files`, so `_has_frontend_files` catches the resulting `AttributeError` and returns `False`, skipping frontend review). ✓
- `cost_check` doesn't trip for either project (both `cost_usd_weekly` start at 0.0 and neither project's `_with_cost` deltas plus the other's checkpointed total reach the default `50.0` threshold from a single trivial task's token cost). ✓

- [ ] **Step 4: Commit**

```bash
git add services/orchestrator/graph/tests/unit/test_concurrency.py
git commit -m "test(orchestrator): prove two interleaved projects don't bleed state

Definition-of-Done proof for TODO.md Phase 18: drives two fixture
projects through the same compiled graph, interleaved, and confirms
neither's plan/messages/project_id ever show up under the other's
thread_id."
```

---

### Task 10: Update TODO.md and ARCHITECTURE.md, tag the phase

**Files:**
- Modify: `TODO.md`
- Modify: `ARCHITECTURE.md`

**Interfaces:**
- Consumes: nothing (documentation-only task).
- Produces: nothing consumed elsewhere — closes out the phase per CLAUDE.md's phase-discipline rule ("At the end of each phase: run the full test suite, commit with message `Phase N: <description>`, update `ARCHITECTURE.md` if any design decision changed").

- [ ] **Step 1: Run the full test suite and record what actually ran**

Run: `pytest --cov=services --cov-fail-under=80 --cov-report=term-missing`

In this sandbox, expect the same pre-existing gap as every phase since Phase 5: suites with no `langgraph`/`langchain-anthropic`/`langchain-ollama`/`fastapi` dependency (this phase's `cost_aggregator.py`, `state.py`, `config.py` tests) run for real; everything under `graph/` and touching `model_router`'s `ChatOllama`/`ChatAnthropic` construction needs those packages installed. Run what you can:

```bash
pytest services/orchestrator/graph/tests/unit/test_cost_aggregator.py \
       services/orchestrator/graph/tests/unit/test_state.py \
       services/orchestrator/tests/unit/test_config.py -v
```

Record the exact pass count for the phase's verification note (Step 2). On a machine with all dependencies installed, run the full command above instead and record that result.

- [ ] **Step 2: Check off Phase 18 in TODO.md and add a verification-status note**

In `TODO.md`, under `## Phase 18 — Multi-Project Concurrency`, change all four `- [ ]` checkboxes to `- [x]`, and add a **Verification status:** paragraph immediately after the **Definition of Done** line, following the exact style of every prior phase's note (see Phase 17's for the pattern) — state plainly which suites ran for real in this sandbox (from Step 1's actual output) versus which are hand-traced pending `langgraph`/`langchain-anthropic`/`langchain-ollama` installation, and name the specific real defect this phase's own validation caught if any turned up while executing this plan (e.g. if `test_integrator.py`'s `build_project_graph()` call site needed no change, say so explicitly rather than leaving it unstated).

- [ ] **Step 3: Add ADR-032 to ARCHITECTURE.md's Decision Log**

In `ARCHITECTURE.md`, add a new row after ADR-031 in the `## 10. Decision Log` table:

```markdown
| 032 | The cost-threshold gate (ADR-009) sums every *other* active project's latest checkpointed `cost_usd_weekly` (a new `cost_aggregator.py` helper reading only each thread's newest checkpoint, not every historical version) plus the current project's own live total, replacing the Phase 5 stub's per-project `cost_usd_session` check entirely; local (Ollama-backed) model calls are serialized through one `threading.Semaphore(1)` per `ModelRouter` instance, wrapped transparently behind `resolve()`'s existing return value so no agent module needed to change (Phase 18) | The stub gate never actually matched ADR-009 ("globally configurable" cost threshold) -- it compared one project's own session cost to a hardcoded constant. `checkpointer.list(None)` returns every historical checkpoint version per thread, not just the latest, so a naive full-history sum would double/triple-count a single project's spend; the aggregator reads only each thread's latest via `get_tuple()`. `threading.Semaphore` (not `asyncio.Semaphore`) is required because every LLM-backed node bridges to its async call via its own `asyncio.run()` (ADR-019) -- concurrent projects each get their own OS thread and event loop, which an `asyncio.Semaphore` scoped to one loop can't coordinate across |
```

Also add one sentence to the model-router narrative section (§4.4, in the paragraph beginning "`resolve(role)` returns a LangChain `BaseChatModel`..."), right after the existing sentence ending "...only what `resolve()` hands back to the caller changed.":

```markdown
Every Ollama-backed model is additionally wrapped in a small internal throttle (`_ThrottledChatModel`) sharing one `threading.Semaphore(1)` per router instance (Phase 18) -- one physical GPU serves every locally-routed role in a deployment, so local calls are serialized while Claude-API-bound calls run in parallel.
```

- [ ] **Step 4: Commit and tag**

```bash
git add TODO.md ARCHITECTURE.md
git commit -m "$(cat <<'EOF'
feat(phase-18): multi-project concurrency

Global cost-threshold gate (sums every active project's checkpointed
cost_usd_weekly instead of one project's own session cost),
threading.Semaphore-based GPU serialization for Ollama-backed calls, and
a thread-isolation test proving no ProjectState bleed between concurrently
active projects (ADR-006).
EOF
)"
git tag phase-18-done
```

---

## Self-Review Notes (for whoever executes this plan)

- **Spec coverage:** Task 3/4/5 cover global cost aggregation; Task 6/7 cover the configurable threshold; Task 8 covers GPU serialization; Task 9 covers the thread-isolation test. All four TODO.md Phase 18 checklist items and both Definition-of-Done sentences are covered.
- **No placeholders:** every step above has real code, not a description of code.
- **Type/name consistency check:** `other_projects_cost_usd_weekly` (Task 3) is the exact name imported in Task 4; `node_cost_check(state, checkpointer, threshold_usd)` (Task 4) is the exact signature `functools.partial`'d in Task 5; `DEFAULT_GLOBAL_COST_THRESHOLD_USD` is defined once in `graph/build.py` (Task 5) and imported by that exact name in Task 7's `service.py` and Task 5's own test; `_ThrottledChatModel(model, semaphore)` (Task 8) matches its one call site in `ModelRouter.__init__` and its two test usages.
- **Out of scope, deliberately not touched:** `cost_usd_weekly` reset/rollover logic, a configurable local-inference concurrency limit above 1, and any explicit lock around `OrchestratorService`'s `graph.invoke()` calls (the design doc's "Out of scope" section explains why each is unnecessary for this phase).
