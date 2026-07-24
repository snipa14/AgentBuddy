# Phase 24 — Human-Gated Replan Route Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The loop can recover from a wrong plan instead of burning its attempt budget and stranding the project — with the human, not the agents, deciding that the plan changes (ADR-047).

**Architecture:** Two triggers route into a new `replan_wait` `interrupt()` node: (1) the Reviewer's verdict schema gains `replan_needed`/`replan_reason`, and (2) a task exhausting `MAX_TASK_ATTEMPTS` routes to replan instead of a terminal halt. Both paths follow the existing producer/wait split (the producer node commits `phase` + `pending_approval`; the wait node only interrupts) and pass through `memory_curator` like every other approval, so curation and routing stay on the single established hub. Approval sends the graph back to `planner_draft` with the failure context injected as a message (picked up by the existing `_extract_description_and_history`), the normal critique + plan-approval gates fire again on the new plan, and `node_task_decomposition` now preserves completed tasks. A `replan_count` state field caps replans at 2; at the cap both triggers fall back to today's behavior.

**Tech Stack:** Python 3.12, Pydantic v2, LangGraph (`interrupt()`/`Command(resume=...)`), pytest.

## Global Constraints

- **Depends on Phase 23 being complete** (the graph already contains `static_checks`; this phase does not touch it).
- The plan-approval gate stays intact: a replan **always** re-enters `planner_critique` → `plan_wait` — never skip or shortcut an `interrupt()` gate (.claude/rules/git.md discipline applies to graph gates the same way).
- Interrupt test contract (.claude/rules/testing.md): every new `interrupt()` needs the three-part test — fires → checkpointer state survives a simulated process restart (new graph instance over the same SQLite file) → resumes to the correct next node.
- Decision vocabulary at the gate is `"approve"`/anything-else, matching the Gateway's single generic Approve/Reject/Edit renderer; the payload needs a `"summary"` key like every other gate.
- Code conventions (CLAUDE.md): no inline comments; Google-style docstrings; no bare `except`; type hints everywhere; `node_` prefix.
- Git: commits directly on `release/v1`; Conventional Commits per task; phase-end commit `Phase 24: human-gated replan route`, tagged `phase-24-done`.
- Sandbox reality: `pytest`/`pydantic` run here; `langgraph`/`langchain_core` do not — `state.py`, `nodes.py`, `build.py`, `reviewer.py` changes and their tests are hand-traced here and executed in CI. Each task states which is which.

---

### Task 1: State schema — `replan_count` and the new phase

**Files:**
- Modify: `services/orchestrator/graph/state.py`
- Test: `services/orchestrator/graph/tests/unit/test_state.py` (additions)

**Interfaces:**
- Produces: `Phase` Literal gains `"awaiting_replan_approval"`; `ProjectState` gains `replan_count: int`; `new_project_state` initializes it to `0`. Tasks 2–4 consume both.

- [ ] **Step 1: Write the failing test additions**

In `test_state.py` (match its existing style):

```python
def test_new_project_state_starts_with_zero_replans():
    state = new_project_state("proj1")
    assert state["replan_count"] == 0


def test_awaiting_replan_approval_is_a_valid_phase():
    state = new_project_state("proj1")
    state["phase"] = "awaiting_replan_approval"
    assert state["phase"] == "awaiting_replan_approval"
```

- [ ] **Step 2: Implement**

In `state.py`: add `"awaiting_replan_approval",` to the `Phase` Literal (after `"awaiting_merge_approval"`); add `replan_count: int` to `ProjectState` (after `changed_files`); add `replan_count=0,` to `new_project_state`'s returned dict with a docstring line: capped at `MAX_REPLANS` (see `graph/nodes.py`) per ADR-047.

- [ ] **Step 3: Verify**

In-sandbox `state.py` imports `langchain_core` → hand-trace; in CI: `python3 -m pytest services/orchestrator/graph/tests/unit/test_state.py -v` → PASS.

- [ ] **Step 4: Commit**

```bash
git add services/orchestrator/graph/state.py services/orchestrator/graph/tests/unit/test_state.py
git commit -m "feat(orchestrator): replan_count state and awaiting_replan_approval phase (ADR-047)"
```

---

### Task 2: Reviewer verdict schema gains the replan signal

**Files:**
- Modify: `services/orchestrator/agents/reviewer.py`
- Test: `services/orchestrator/agents/tests/unit/test_reviewer.py` (additions)

**Interfaces:**
- Produces: `ReviewResult.replan_needed: bool = False` and `ReviewResult.replan_reason: str = ""`. Task 3's `node_reviewer` consumes them.

- [ ] **Step 1: Write the failing test additions**

In `test_reviewer.py` (structure-not-content per .claude/rules/testing.md; mock at the `model_router.resolve()` boundary exactly as the file already does):

```python
def test_review_result_defaults_to_no_replan():
    result = ReviewResult(verdict="approve")
    assert result.replan_needed is False
    assert result.replan_reason == ""


def test_review_result_carries_replan_signal():
    result = ReviewResult(
        verdict="changes-requested",
        replan_needed=True,
        replan_reason="Task 3 assumes an API endpoint the plan never provisions",
    )
    assert result.replan_needed is True
    assert result.replan_reason
```

- [ ] **Step 2: Implement**

In `ReviewResult`:

```python
    replan_needed: bool = Field(
        default=False,
        description=(
            "True only when a finding cannot be fixed by editing the diff "
            "because the underlying plan task rests on a false assumption"
        ),
    )
    replan_reason: str = Field(
        default="", description="Why the plan itself is wrong, when replan_needed"
    )
```

Extend `_REVIEW_SYSTEM_PROMPT` with one paragraph: if an issue is not fixable within the current plan — a task's premise is false, a dependency the plan assumed doesn't exist, the approach is structurally wrong — set `replan_needed` true with a concrete `replan_reason`, and still report the findings; otherwise leave `replan_needed` false. Update the class docstring.

- [ ] **Step 3: Verify**

Hand-trace in-sandbox (`reviewer.py` imports `langchain_core`); CI: `python3 -m pytest services/orchestrator/agents/tests/unit/test_reviewer.py -v` → PASS. The 5-run reliability harness in `services/orchestrator/agents/tests/integration/test_reviewer_reliability.py` is **not** extended for the replan signal — replan triggering is a rare judgment call, not a plant-a-defect reliability target; note this in the commit message.

- [ ] **Step 4: Commit**

```bash
git add services/orchestrator/agents/reviewer.py services/orchestrator/agents/tests/unit/test_reviewer.py
git commit -m "feat(orchestrator): reviewer verdict gains replan_needed signal (ADR-047)"
```

---

### Task 3: Nodes and routing — both triggers, the wait node, preserved tasks

**Files:**
- Modify: `services/orchestrator/graph/nodes.py`
- Test: `services/orchestrator/graph/tests/unit/test_nodes.py` (additions)

**Interfaces:**
- Consumes: Task 1's `replan_count`/phase; Task 2's `ReviewResult` fields; existing `MAX_TASK_ATTEMPTS`, `_replace_task`, producer/wait split conventions.
- Produces: `MAX_REPLANS = 2`; `node_replan_wait`; `route_after_replan_wait`; changed `node_coder` (exhaustion branch), `node_reviewer` (replan branch), `route_after_reviewer` (new `"memory_curator"` target), `node_task_decomposition` (preserves done tasks), `route_after_memory_curator` (two new routes). Task 4 wires `replan_wait` into `build.py`.

- [ ] **Step 1: Write the failing test additions**

Add to `test_nodes.py`, adapting to its existing state/stub helpers:

```python
def test_coder_exhaustion_routes_to_replan_when_under_cap(...):
    """Third failed attempt: task blocked, but phase becomes
    awaiting_replan_approval with a replan payload, not a terminal halt."""
    # state: one task at attempts=2 in_progress, replan_count=0; coder result: failure
    # assert update["phase"] == "awaiting_replan_approval"
    # assert update["task_queue"][0]["status"] == "blocked"
    # assert update["curator_source"] == "coder"
    # assert update["pending_approval"]["kind"] == "replan_approval"
    # assert "summary" in update["pending_approval"]


def test_coder_exhaustion_at_replan_cap_blocks_as_before(...):
    # same but state["replan_count"] = 2
    # assert update["phase"] == "blocked" and update.get("pending_approval") is None


def test_reviewer_replan_needed_arms_replan_gate(...):
    # mocked review_changes returns ReviewResult(verdict="changes-requested",
    #   replan_needed=True, replan_reason="false assumption"), replan_count=0
    # assert update["phase"] == "awaiting_replan_approval"
    # assert update["pending_approval"]["kind"] == "replan_approval"
    # assert "false assumption" in update["pending_approval"]["summary"]


def test_reviewer_replan_needed_at_cap_degrades_to_changes_requested(...):
    # same mocked result but replan_count=2
    # assert update["phase"] == "implementing"
    # assert "false assumption" in update["messages"][0].content


def test_replan_wait_approve_returns_to_planning_and_increments_count(...):
    # patch langgraph interrupt -> "approve" (same technique the file's other
    # wait-node tests use); state with pending_approval payload
    # assert update["phase"] == "planning"
    # assert update["replan_count"] == 1
    # assert update["curator_source"] == "replan_wait"
    # assert update["pending_approval"] is None
    # assert "replan" in update["messages"][0].content.lower()


def test_replan_wait_reject_blocks(...):
    # interrupt -> "reject"
    # assert update["phase"] == "blocked"
    # assert update["curator_source"] == "replan_wait"


def test_task_decomposition_preserves_done_tasks(...):
    # state.task_queue = [done task "keep me", blocked task "drop me"];
    # mocked decompose_plan_into_tasks returns two new tasks
    # assert queue[0]["description"] == "keep me" and queue[0]["status"] == "done"
    # assert all blocked entries dropped, new tasks appended as pending/attempts 0


def test_route_after_reviewer_sends_replan_to_curator(...):
    # phase awaiting_replan_approval -> "memory_curator"


def test_route_after_memory_curator_replan_routes(...):
    # phase awaiting_replan_approval (any source) -> "replan_wait"
    # source "replan_wait" + phase "planning" -> "planner_draft"
```

Write these as real tests (full bodies following the file's existing patterns), not comments — the sketches above pin the required assertions.

- [ ] **Step 2: Implement in `nodes.py`**

Add `MAX_REPLANS = 2` beside `MAX_TASK_ATTEMPTS` with a docstring-style module note in the module docstring's loop-budget paragraph.

**Helper** (private, above `node_coder`):

```python
def _replan_payload(state: ProjectState, reason: str, source: str) -> dict[str, Any]:
    """Builds the replan_wait interrupt payload (ADR-047).

    Args:
        state: The current project state.
        reason: Why a replan is being proposed (reviewer reason or the
            blocked task's description and attempt history).
        source: Which trigger produced this -- "reviewer" or "coder".

    Returns:
        A pending_approval dict for the Gateway's generic renderer.
    """
    return {
        "kind": "replan_approval",
        "thread_id": state["project_id"],
        "project_id": state["project_id"],
        "trigger": source,
        "reason": reason,
        "replan_count": state["replan_count"],
        "summary": (
            f"Plan appears broken ({source}): {reason} -- approve a replan? "
            f"({state['replan_count']}/{MAX_REPLANS} replans used)"
        ),
    }
```

**`node_coder` exhaustion branch** — replace the current `attempts >= MAX_TASK_ATTEMPTS` block's return with:

```python
    if active_task["attempts"] >= MAX_TASK_ATTEMPTS:
        active_task["status"] = "blocked"
        new_queue = _replace_task(task_queue, active_index, active_task)
        if state["replan_count"] < MAX_REPLANS:
            logger.warning(
                "project %s: task exhausted %d attempts, proposing replan",
                state["project_id"], active_task["attempts"],
            )
            reason = (
                f"task {active_task['description']!r} failed "
                f"{active_task['attempts']} attempts"
            )
            return _with_cost(
                state,
                {
                    "task_queue": new_queue,
                    "phase": "awaiting_replan_approval",
                    "curator_source": "coder",
                    "changed_files": new_changed_files,
                    "pending_approval": _replan_payload(state, reason, "coder"),
                },
                result.cost_usd,
            )
        logger.warning(
            "project %s: task exhausted %d attempts at replan cap, blocking",
            state["project_id"], active_task["attempts"],
        )
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

`route_after_coder` needs no change for this: phase `awaiting_replan_approval` is neither `blocked` nor `reviewing`, so add an explicit branch —

```python
    if state["phase"] == "awaiting_replan_approval":
        return "memory_curator"
```

before the fallthrough `return "coder"` (and update its docstring).

**`node_reviewer`** — after `cost` is computed, before the `changes-requested` branch:

```python
    if result.replan_needed and state["replan_count"] < MAX_REPLANS:
        logger.warning(
            "project %s: reviewer proposes replan: %s",
            state["project_id"], result.replan_reason,
        )
        return _with_cost(
            state,
            {
                "phase": "awaiting_replan_approval",
                "curator_source": "reviewer",
                "pending_approval": _replan_payload(
                    state, result.replan_reason, "reviewer"
                ),
            },
            cost,
        )
    if result.replan_needed:
        logger.warning(
            "project %s: reviewer proposed replan at cap, degrading to revision",
            state["project_id"],
        )
        result = result.model_copy(
            update={
                "verdict": "changes-requested",
                "findings": list(result.findings)
                + [
                    ReviewFinding(
                        file="(plan)", line=0, severity="critical",
                        message=f"replan cap reached: {result.replan_reason}",
                    )
                ],
            }
        )
```

(import `ReviewFinding` alongside the existing reviewer imports). Docstring: document both branches and the cap-degrade behavior.

`route_after_reviewer` gains:

```python
    if state["phase"] == "awaiting_replan_approval":
        return "memory_curator"
```

**`node_replan_wait`** (beside the other wait nodes):

```python
def node_replan_wait(state: ProjectState) -> dict[str, Any]:
    """Pauses for a human decision on redrafting the plan (ADR-047).

    Approve sends the graph back to the Planner's draft step with the
    failure context injected into messages -- the redrafted plan then goes
    through the normal critique and plan-approval gates again, so a human
    still signs off on the actual new plan. Anything else halts exactly as
    a blocked project does today. replan_count is incremented here, the
    single place a replan is actually granted.
    """
    decision = interrupt(state["pending_approval"])
    if decision != "approve":
        return {
            "phase": "blocked",
            "curator_source": "replan_wait",
            "pending_approval": None,
        }
    payload = state["pending_approval"] or {}
    context = (
        "Replan approved. The previous plan failed: "
        f"{payload.get('reason', 'unknown reason')} (trigger: "
        f"{payload.get('trigger', 'unknown')}). Draft a corrected plan; "
        "completed tasks will be preserved and must not be re-planned."
    )
    return {
        "phase": "planning",
        "curator_source": "replan_wait",
        "replan_count": state["replan_count"] + 1,
        "messages": [HumanMessage(content=context)],
        "pending_approval": None,
    }


def route_after_replan_wait(state: ProjectState) -> str:
    """Always routes through memory_curator -- see route_after_plan_wait's
    docstring for why blocked isn't short-circuited here."""
    return "memory_curator"
```

**`route_after_memory_curator`** — add, before the existing `phase == "implementing"` branch:

```python
    if state["phase"] == "awaiting_replan_approval":
        return "replan_wait"
    if source == "replan_wait" and state["phase"] == "planning":
        return "planner_draft"
```

and extend its docstring's route list. (`phase == "blocked"` already returns END first, covering the reject path.)

**`node_task_decomposition`** — preserve completed work:

```python
    done_tasks = [task for task in state["task_queue"] if task["status"] == "done"]
    new_tasks = [
        {"description": task.description, "status": "pending", "attempts": 0}
        for task in tasks
    ]
    task_queue = done_tasks + new_tasks
```

Docstring gains: on a replan, previously completed tasks are carried forward as `done` and blocked/pending entries from the failed plan are dropped (ADR-047); on first decomposition `task_queue` is empty so this is a no-op. Also pass the completed descriptions into the decomposition prompt: change `decompose_plan_into_tasks(router, state["plan"])` to `decompose_plan_into_tasks(router, state["plan"], completed_task_descriptions=[t["description"] for t in done_tasks])` — see Step 3.

- [ ] **Step 3: Extend `decompose_plan_into_tasks` in `agents/coder.py`**

Add an optional keyword `completed_task_descriptions: list[str] | None = None`; when non-empty, append to the decomposition prompt: `"The following tasks are already complete and must not be re-planned:\n"` + one bullet per description. Update its docstring and the existing `test_coder.py` decomposition tests (they keep passing — the argument is optional — plus one new test asserting the prompt's `HumanMessage` contains the completed bullet when provided; structure-not-content: assert containment of the description string the test itself supplied).

- [ ] **Step 4: Verify**

In-sandbox: hand-trace every new test against the implementation, including the LangGraph replay rule (the interrupt is the sole statement pattern: `node_replan_wait` reads only state, no LLM/Executor calls before `interrupt()`). In CI: `python3 -m pytest services/orchestrator/graph/tests/unit/test_nodes.py services/orchestrator/agents/tests/unit/test_coder.py -v` → PASS.

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/graph/nodes.py services/orchestrator/agents/coder.py services/orchestrator/graph/tests/unit/test_nodes.py services/orchestrator/agents/tests/unit/test_coder.py
git commit -m "feat(orchestrator): replan triggers, replan_wait gate, preserved tasks (ADR-047)"
```

---

### Task 4: Graph wiring + Gateway payload check

**Files:**
- Modify: `services/orchestrator/graph/build.py`
- Verify (modify only if needed): `services/telegram_gateway/rendering.py`

**Interfaces:**
- Consumes: Task 3's node/route functions.
- Produces: the compiled graph containing `replan_wait`, reachable from both triggers.

- [ ] **Step 1: Wire `replan_wait` in `build.py`**

Import `node_replan_wait`, `route_after_replan_wait`. Add `builder.add_node("replan_wait", node_replan_wait)`. Add:

```python
    builder.add_conditional_edges(
        "replan_wait", route_after_replan_wait, {"memory_curator": "memory_curator"}
    )
```

Extend the reviewer mapping with `"memory_curator": "memory_curator"` and the memory_curator mapping with `"replan_wait": "replan_wait"` and `"planner_draft": "planner_draft"`. Update the builder docstring's chain description (exhausted retries / reviewer replan → `replan_wait` → planner redraft → normal plan approval).

- [ ] **Step 2: Verify the Gateway renders the new payload kind**

Read `services/telegram_gateway/rendering.py`. Expected: the generic Approve/Reject/Edit renderer keys off `pending_approval["summary"]` and needs no change (the cost gate's docstring in `nodes.py` says all gates share one rendering path). If it instead branches on `kind`, add a `"replan_approval"` branch mirroring `"cost_alert"`'s and extend `services/telegram_gateway/tests/unit/test_rendering.py` accordingly (that suite runs in-sandbox if it avoids aiogram imports; otherwise hand-trace).

- [ ] **Step 3: Commit**

```bash
git add services/orchestrator/graph/build.py
git commit -m "feat(orchestrator): wire replan_wait interrupt into the project graph (ADR-047)"
```

(Include the gateway files in the same commit if Step 2 required changes.)

---

### Task 5: Interrupt-contract test + phase close-out

**Files:**
- Modify: `services/orchestrator/graph/tests/unit/test_graph_end_to_end.py` (additions)
- Modify: `TODO.md`, `ARCHITECTURE.md` (§5 state schema + graph description; ADR-047 already exists)

**Interfaces:**
- Consumes: everything above.

- [ ] **Step 1: Write the interrupt-contract test (.claude/rules/testing.md, all three parts)**

Follow the file's existing interrupt tests (plan_wait/cost_wait/merge_wait) as the pattern — same checkpointer construction, same `Command(resume=...)` technique:

```python
def test_replan_interrupt_fires_survives_restart_and_resumes(...):
    """ADR-047 DoD: a project whose plan rests on a false assumption reaches
    replan_wait, survives a restart there, and on approval produces a new
    plan that goes through normal plan approval with done tasks preserved."""
```

Mechanics: drive a graph (stub router: coder agent result always `success=False`, so one task exhausts `MAX_TASK_ATTEMPTS`) with a SQLite checkpointer file in `tmp_path`. Assert: (1) the run pauses with `pending_approval["kind"] == "replan_approval"` and state phase `awaiting_replan_approval`; (2) build a **new** graph instance over the same SQLite file and assert `get_state` still shows the armed payload (simulated restart); (3) resume with `Command(resume="approve")` on the new instance and assert the graph proceeds to `planner_draft` (stub planner returns a fresh plan) and pauses again at the **plan-approval** interrupt (`pending_approval["kind"] == "plan_approval"`), with `replan_count == 1` and every previously-done task still `status == "done"` in `task_queue`.

```python
def test_replan_reject_halts(...):
    # resume with Command(resume="reject"); assert phase "blocked" and the
    # run terminates (END) via memory_curator.


def test_third_replan_attempt_halts(...):
    # seed state with replan_count == MAX_REPLANS before exhaustion;
    # assert the graph blocks without arming replan_wait.
```

- [ ] **Step 2: Verify**

In-sandbox: hand-trace all three against `build.py` + `nodes.py`, explicitly walking the checkpointer-restart step (what LangGraph persists at an interrupt and what `get_state` returns on a fresh instance). In CI: `python3 -m pytest services/orchestrator/graph/tests/unit/test_graph_end_to_end.py -v` → PASS.

- [ ] **Step 3: Phase close-out**

Run everything runnable in-sandbox (`agents`, `registry`, `executor` unit suites) → zero FAIL. Check Phase 24's TODO.md boxes with verification notes. Update ARCHITECTURE.md: §5's ProjectState field list gains `replan_count`; the graph/interrupt description gains `replan_wait` (ADR-047 row already exists — no new ADR). Commit and tag:

```bash
git add TODO.md ARCHITECTURE.md services/orchestrator/graph/tests/unit/test_graph_end_to_end.py
git commit -m "Phase 24: human-gated replan route"
git tag phase-24-done
```
