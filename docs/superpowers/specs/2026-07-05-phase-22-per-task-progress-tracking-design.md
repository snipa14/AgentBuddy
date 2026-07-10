# Phase 22 (stretch item) — Per-Task Structured Progress Tracking: Design

**Status:** Approved
**Phase:** TODO.md Phase 22 ("Per-task structured progress tracking")
**Decisions:** New ADR (to be numbered in ARCHITECTURE.md §10 during implementation)

## Goal

Replace `task_queue`'s implicit "completion = removal from the list" semantics with an
explicit, machine-readable per-task status, so a resumed session, the Memory Curator, or a
`/status` Telegram query can see exactly which tasks are pending, in progress, done, or
blocked — per Anthropic's published guidance on long-running coding-agent harnesses
(TODO.md line 451), which specifically calls out a persistent, machine-readable task list
with per-item status as what prevents an agent from losing track of progress.

## Current State

- `ProjectState.task_queue: list[dict]` (`services/orchestrator/graph/state.py:43`) holds
  items shaped `{"description": str}`, produced by `CoderTask.model_dump()`
  (`services/orchestrator/agents/coder.py:43-46`).
- `ProjectState.current_task: dict | None` (`state.py:44`) is a free-form copy of the active
  item plus a bolted-on `attempts` counter, promoted from `task_queue[0]`.
- `node_coder` (`graph/nodes.py:248-308`) pops the head off `task_queue` via slicing
  (`task_queue[1:]`) on success. On failure it mutates `current_task["attempts"]` in place
  and either retries or sets `phase: "blocked"`.
- **Existing bug this design fixes:** when a task exhausts `MAX_TASK_ATTEMPTS`, it is left
  sitting in `current_task` *and* still present, unchanged, as `task_queue[0]` — the same
  logical task exists in two places with no shared status.
- `GET /status/{project_id}` (`services/orchestrator/api.py`, `service.py:116-135`) returns
  only `{project_id, phase, pending_approval}` — no task-level detail reaches the Gateway.
- `node_memory_curator` (`graph/nodes.py:640-699`) curates only the last 20 chat `messages`;
  it never reads `task_queue`, so `PROJECT_MEMORY.md`/`LEARNINGS.md` carry no record of which
  tasks succeeded, failed, or were retried.
- `ARCHITECTURE.md` §5 documents `task_queue`/`current_task` with no elaboration of their
  contents, and is already stale relative to the real `ProjectState` (missing `stack`,
  `git_branch`, `git_remote_url`, `curator_source`).

## Decision: Keep All Tasks In `task_queue`, Drop `current_task`

`task_queue` becomes the complete, ordered list of every task for the current implementation
phase, for the lifetime of that phase. Items transition `pending → in_progress → done`, or
`in_progress → blocked`, in place. Nothing is ever removed or sliced out.

`current_task` is deleted from `ProjectState`. The "active" task is found by scanning
`task_queue` for the single item with `status == "in_progress"` (there is at most one at any
point, since `node_coder` processes tasks strictly in order).

**Why this over keeping `current_task` or removing tasks on completion:** a full in-place
status list is what actually satisfies the "machine-readable task list" goal — a shrinking
queue still throws away history, and a queue-plus-`current_task` pair still has two sources
of truth for the same logical entity, which is exactly the bug that let a blocked task go
duplicated. One list, one status field, no duplication.

## Schema Changes (`services/orchestrator/graph/state.py`)

```python
TaskStatus = Literal["pending", "in_progress", "done", "blocked"]
```

added alongside the existing `Phase`/`Mode`/`Stack`/`PrMode`/`CoverageGateMode` literals.

`ProjectState`:
- `task_queue: list[dict]` — unchanged type, new item shape (see below).
- `current_task: dict | None` — **removed**.

`new_project_state()`: drop the `current_task=None` line; `task_queue=[]` unchanged.

`CoderTask` (`services/orchestrator/agents/coder.py`):

```python
class CoderTask(BaseModel):
    """One item from a decomposed plan's task_queue."""
    description: str
    status: TaskStatus = "pending"
    attempts: int = 0
```

Both new fields default so the LLM structured-output schema (`_TaskList`) is unaffected —
the model only ever produces `description`; `status`/`attempts` are filled in by
`model_dump()`'s defaults at decomposition time.

## `node_coder` Rewrite (`services/orchestrator/graph/nodes.py`)

Replaces the current head-of-queue-pop / `current_task`-mutation logic:

1. Find the active task: the `task_queue` item with `status == "in_progress"`. If none,
   take the first item with `status == "pending"` and set its `status` to `"in_progress"`
   before running it.
2. If no `pending` or `in_progress` item exists at all, return `{"phase": "reviewing"}`
   (replaces today's `if not task_queue: return {"phase": "reviewing"}` check — the queue is
   never empty once decomposition has run, so this is now a status check, not a length
   check).
3. Run `run_coder_task(...)` against the active item's `description`, exactly as today.
4. On success: set that item's `status` to `"done"`. New `phase` is `"implementing"` if any
   task is still `pending`/`in_progress`, else `"reviewing"`.
5. On failure: increment that item's `attempts`. If `attempts >= MAX_TASK_ATTEMPTS`, set
   `status` to `"blocked"` and `phase` to `"blocked"` (same threshold and
   `curator_source: "coder"` behavior as today). Otherwise leave `status` as `"in_progress"`
   for the next retry, `phase` stays `"implementing"`.

`route_after_coder` is unchanged — it branches only on `state["phase"]`, which keeps its
existing three outcomes (`"blocked"` → memory_curator, `"reviewing"` → reviewer, else loop to
`"coder"`).

## API + Telegram Surfacing

`services/orchestrator/api.py`: `ProjectStatusResponse` gains a `task_progress` field:

```python
task_progress: dict | None  # {"done": int, "total": int, "tasks": [{"description": str, "status": TaskStatus, "attempts": int}, ...]}
```

`OrchestratorService.status()` (`service.py:116-135`) populates it from
`snapshot.values["task_queue"]` when present (`None` before decomposition has run, i.e.
during `intake`/`planning`/`roasting`/`awaiting_plan_approval`).

Telegram Gateway's status-rendering path (wherever the `/status` reply is composed, from
`GatewayClient.get_status()`'s response) is extended to show a compact progress summary —
e.g. `"3/7 tasks done"` plus the description of whichever task is `in_progress` or
`blocked` — instead of just the bare `phase` string.

## Memory Curator Integration

`node_memory_curator` builds a per-task summary line for every item in the phase's
`task_queue` — `"[status] description (attempts: N)"` — and includes it alongside the
existing last-20-messages excerpt as input to curation, so `PROJECT_MEMORY.md`/
`LEARNINGS.md` entries reflect actual task outcomes, not just incidental chat mentions.

## Testing

- `graph/tests/unit/test_state.py`: drop the `state["current_task"] is None` assertion;
  default-state check for `task_queue == []` unchanged.
- `graph/tests/unit/test_nodes.py`: rewrite the ~9 hand-built `task_queue`/`current_task`
  dict literals to the new shape (`{"description": ..., "status": ..., "attempts": ...}`,
  no `current_task` key anywhere). Replace
  `test_node_task_decomposition_populates_task_queue`'s literal full-dict equality with a
  shape-tolerant check that also asserts `status == "pending"` and `attempts == 0` on the
  decomposed items.
- New tests: a multi-task queue progressing task-by-task (`pending` → `in_progress` →
  `done`, next task picked up next call); a retry incrementing `attempts` in place while
  `status` stays `"in_progress"`; exhausted retries landing in `"blocked"` while the item
  remains visible (and correctly shaped) in `task_queue`; the new `task_progress` field on
  `GET /status/{project_id}`.
- `agents/tests/unit/test_coder.py`: `CoderTask(description=...)` construction sites are
  unaffected (new fields are defaulted); add a small assertion that defaults are
  `status="pending"`, `attempts=0`.
- `graph/tests/unit/test_concurrency.py`: unaffected — it drives `task_queue` only through
  `CoderTask` fixtures, never raw dict literals.

## ARCHITECTURE.md Update

§5 (State Schema) gets a corrected `ProjectState` code block: `current_task` removed,
`task_queue`'s item shape documented explicitly (`description`, `status`, `attempts`), and
the pre-existing drift (missing `stack`, `git_branch`, `git_remote_url`, `curator_source`)
reconciled in the same pass since the section is being touched anyway. §10 Decision Log gets
a new ADR entry recording this change and its rationale (one list, one status field, no
duplication).

## Out of Scope

- **No checkpoint migration** for already-persisted `ProjectState` under the old shape —
  pre-v1, no real running project depends on the old `current_task`/pop-based format, and
  `projects/*/` runtime state is gitignored.
- **No "unblock and resume a specific task" flow.** A task that lands in `"blocked"` still
  halts the phase exactly as today (`phase: "blocked"`, routed to `memory_curator`); giving a
  human or the Planner a way to edit/retry/skip that specific task is the separate
  "mid-implementation re-planning path" stretch item in TODO.md Phase 22, not this one.
