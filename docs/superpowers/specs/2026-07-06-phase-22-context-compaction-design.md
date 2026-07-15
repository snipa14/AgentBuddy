# Phase 22 stretch — Context management / compaction strategy

Status: approved (design phase), not yet implemented.

## Problem

TODO.md Phase 22 flags a gap: "no phase addresses what happens when
`messages`, `task_queue`, or a project's own file tree grow large enough to
approach a provider's context window on a long-running or brownfield
project -- every agent node currently reconstructs its prompt from full,
uncompacted state on every call."

Tracing the actual production code (not just the TODO's framing) narrows
this to three concrete, independent growth vectors:

1. **`messages`** grows forever via `ProjectState`'s `Annotated[list[BaseMessage], add]`
   reducer -- nothing has ever removed an entry. Only two production call
   sites read it: `_extract_description_and_history` (`graph/nodes.py`,
   feeding `node_planner_ask`/`node_planner_draft`/`node_planner_critique`),
   which reads **all** of `messages[1:]` every interview round with no cap,
   and `node_memory_curator`, which already reads a hardcoded `messages[-20:]`
   window for its own prompt -- a read-only slice that never shrinks the
   underlying list. The real graph has no interview-round cap at all (the
   `max_rounds=5` safety cap on `agents/planner.py`'s `run_intake_to_final_plan`
   is a reference implementation the real LangGraph nodes deliberately don't
   call -- see that module's docstring).
2. **`task_queue`** is deliberately never shrunk (ADR-034) -- bounded
   per-call cost today since `node_coder` only ever reads the one active
   task, but `_task_progress_lines()` renders **every** task into the Memory
   Curator's prompt every time it runs, so a long-lived project's curator
   prompt grows with total task count forever.
3. **Reviewer's file-tree read** is the sharpest vector: `review_changes()`
   (`agents/reviewer.py`) already accepts a `changed_files` parameter to
   scope review to specific paths, but `node_reviewer` never passes it --
   it always calls with `changed_files=None`, which lists and reads **every
   file in the whole project** into one prompt on **every single review
   pass**, regardless of project size. This is not exclusive to "long-running"
   projects; it degrades on any moderately-sized repo today.

## Scope

All three vectors, in one design, since they share the same root cause
(nodes reconstructing prompts from full, uncompacted state) and the fixes
are otherwise independent and low-risk to land together.

Out of scope, explicitly:
- Re-summarizing `messages_summary` itself if it ever grows large over an
  extremely long-lived project. Accepted as a known limitation (see below),
  not solved here.
- Detecting file changes made via `run_command` shell redirects instead of
  the `write_file` tool. Accepted as a known limitation (see below).
- A hard cap on Planner interview rounds. Related (same self-loop that
  drives `messages` growth) but a distinct "infinite loop safety" concern,
  not a compaction concern -- not addressed by this design.

## Design

### Schema changes (`graph/state.py`)

Three new plain fields on `ProjectState` (no reducer -- same "read current
value, write the new total" idiom this codebase already uses for
`cost_usd_session`/`cost_usd_weekly` via `_with_cost`), initialized in
`new_project_state()`:

- `messages_summary: str` -- default `""`. Rolling curated-summary text,
  the compacted stand-in for every message already folded in.
- `messages_curated_through: int` -- default `0`. Index into `messages`;
  everything at or after this index hasn't been curated yet.
- `changed_files: list[str]` -- default `[]`. Every file path the Coder has
  actually written this project, deduped, accumulated across all tasks and
  retries.

### Messages compaction, curator-driven

`node_memory_curator` already only fires at real phase-transition
checkpoints (`plan_wait`, `testing`, `merge_wait`, the Coder's blocked
path), so it's the natural place to both consume and shrink the backlog.

Change its `phase_events` source from the current hardcoded
`state["messages"][-20:]` to `state["messages"][state["messages_curated_through"]:]`
-- exactly what's accumulated since the last checkpoint, with no arbitrary
window and no risk of silently skipping events that piled up beyond 20
between checkpoints.

On a **successful** curation -- inside the existing `try`, at the same
`return` that already prices the call's cost, never inside the `except`
-- fold the result into the compaction fields:

```python
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
```

Keeping this inside the success path only matters: a failed curation call
(the existing `except Exception: return {}` path) must neither lose those
events nor falsely mark them as already curated -- exactly the same
invariant the existing code already relies on for `LEARNINGS.md`/global
memory writes, just now extended to the new compaction fields.

`_extract_description_and_history` (`graph/nodes.py`) changes from reading
all of `messages[1:]` to:

```python
def _extract_description_and_history(state: ProjectState) -> tuple[str, list[str]]:
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

`messages[0]` (the stakeholder's original request) is always kept verbatim
-- it is never summarized away, matching current behavior exactly. Only the
"answers/events so far" portion is compacted. `max(1, ...)` guards against
the cursor ever pointing at or before index 0 (e.g. its default `0`), so the
description is never accidentally duplicated into `qa_history`.

**Accepted limitation:** `messages_summary` grows slower than raw
`messages` (one short paragraph per phase transition instead of per raw
message) but not never -- an extremely long-lived project could eventually
need its own re-summarization pass over `messages_summary` itself. Not
building that now; it is a natural, separate follow-up if it ever actually
becomes a problem, not a hypothetical to design around today.

### Reviewer file-tree scoping

`agents/coder.py`'s `CoderTaskResult` gains a new field:

```python
class CoderTaskResult(BaseModel):
    success: bool
    transcript: list[str]
    written_files: list[str] = []
    cost_usd: float = 0.0
```

`run_coder_task` captures the path directly off any dispatched `write_file`
tool call (not parsed back out of the free-text `transcript` strings, which
are for logging/tests, not structured data):

```python
for call in ai_message.tool_calls:
    ...
    if call["name"] == "write_file":
        written_files.append(call["args"]["path"])
    ...
return CoderTaskResult(success=True, transcript=transcript, written_files=written_files, cost_usd=total_cost)
```

(`written_files` initialized as `[]` alongside the existing `transcript`
list at the top of the function; both the success and max-steps-exhausted
return paths include it.)

`node_coder` (`graph/nodes.py`) merges each attempt's `written_files` into
`state["changed_files"]` -- deduped, order-preserving -- on **every** path
through the node: success, retry, and blocked alike, since even a failed
attempt may have written files worth re-reviewing. This becomes part of
every state update `node_coder` already returns (success/retry/blocked
branches), not a separate node.

`node_reviewer` changes from:

```python
result, ai_message = asyncio.run(review_changes(router, executor_client, state["project_id"]))
```

to:

```python
result, ai_message = asyncio.run(
    review_changes(router, executor_client, state["project_id"], changed_files=state["changed_files"])
)
```

`review_changes()` itself is unchanged -- it already branches correctly on
a populated `changed_files` list; this just wires up the previously-unused
parameter with real data instead of leaving it `None` at every call site.

**Accepted limitation, stated plainly rather than solved:** this only
tracks files touched via the `write_file` tool. Nothing technically
prevents a model from mutating a file through `run_command` instead (e.g. a
shell redirect), which would escape review scoping entirely. The Coder's
own system prompt already steers it toward `write_file` for edits ("Use the
read_file/write_file/run_command tools as needed to implement and verify
the task"), but doesn't enforce it. Not building drift-detection or a
periodic full-repo sweep for this now -- flagged here as a known,
documented tradeoff to hold onto for the next iteration if it ever proves
to matter in practice, not a hypothetical to solve preemptively.

### Task_queue → curator prompt bound

`_task_progress_lines()` (`graph/nodes.py`) changes to keep every
non-`"done"` task (`pending`/`in_progress`/`blocked` -- always
actionable/interesting) plus only the most recent 10 `"done"` entries, by
original order, instead of rendering every task the project has ever had:

```python
def _task_progress_lines(task_queue: list[dict], recent_done_limit: int = 10) -> list[str]:
    done_indices_to_keep = {
        i for i, task in enumerate(task_queue) if task["status"] == "done"
    }
    done_indices_to_keep = set(sorted(done_indices_to_keep)[-recent_done_limit:])
    kept = [
        task
        for i, task in enumerate(task_queue)
        if task["status"] != "done" or i in done_indices_to_keep
    ]
    return [
        f"[{task['status']}] {task['description']} (attempts: {task['attempts']})"
        for task in kept
    ]
```

## Testing

Same conventions as every other node/agent test in this codebase
(FakeRouter-backed unit tests, hand-traced rather than executed --
`langgraph` is still not installed in this sandbox, a permanent gap since
Phase 5). New/changed coverage:

- `node_memory_curator` advances `messages_summary`/`messages_curated_through`
  only when curation succeeds, never when the curator's `except Exception`
  path fires.
- `_extract_description_and_history` correctly combines a populated
  `messages_summary` with the post-cursor tail, and never duplicates
  `messages[0]` into `qa_history`.
- `_task_progress_lines()` keeps all non-done tasks plus only the most
  recent `recent_done_limit` done tasks.
- `run_coder_task` returns `written_files` matching dispatched `write_file`
  calls, on both the success and max-steps-exhausted paths.
- `node_coder` accumulates `changed_files` across multiple tasks and across
  retries of the same task, deduped.
- `node_reviewer` passes `state["changed_files"]` through to
  `review_changes()` instead of calling it with no `changed_files` arg.

## Non-goals recap

- No reducer change (`add`/`add_messages`/`RemoveMessage`) -- the additive
  summary-field approach avoids depending on unverifiable `langgraph`/
  `BaseMessage` id behavior in this sandbox and matches this codebase's
  existing "plain field, read-then-write" idiom instead of introducing a
  new one.
- No interview-round hard cap.
- No drift detection for `run_command`-based file writes.
- No re-summarization of `messages_summary` itself.
