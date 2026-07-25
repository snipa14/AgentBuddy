# Design: Telegram conversational loop (free-text interview resume)

Date: 2026-07-25
Status: approved

## Problem

Answering the Planner's clarifying questions via plain Telegram text doesn't
work: every free-text message hits `POST /webhook` carrying only `user_id`
and `text` (never a `project_id`), and `OrchestratorService.handle_webhook`
treats a missing `project_id` as "start a brand-new project" unconditionally.
So each typed answer spins up a fresh project with that answer as its own
initial description, which itself immediately asks new clarifying
questions — from the user's side this looks like "it keeps re-asking the
same thing." This is a known, pre-existing gap, not introduced by this
change: `handle_webhook`'s own docstring already says feeding an existing
thread's free text into the graph was deliberately deferred to "whichever
phase builds the full conversational Telegram loop" (Phase 5/8), and no
later phase closed it (confirmed via TODO.md).

A second, related symptom: `render_approval` shows the same Approve/Reject/
Edit keyboard for the interview's `pending_approval` (`kind: "plan_questions"`)
as it does for real approval gates, even though those buttons don't map to
anything meaningful for an interview answer — and pressing one currently
*does* resume the graph, feeding the literal string "approve"/"reject"/"edit"
to the Planner as if it were the stakeholder's answer.

## Decision

Track one "active project" pointer server-side, and use the `"kind"` field
every interrupt payload already carries (`plan_questions` vs `plan_approval`/
`merge_approval`/`replan_approval`/`cost_alert`) to decide whether incoming
free text should resume that project's interview. No graph-layer changes:
`node_planner_ask`/`node_planner_wait` and their `interrupt()` already work
correctly — this design only adds the server-side routing that was missing.

Rejected alternatives:
- **Telegram reply-to-message threading only** (no server-side active-project
  state): would correctly handle N simultaneously-paused projects, but a
  bare typed message has no target unless the user always manually invokes
  Telegram's native reply feature. Explicitly ruled out.
- **An out-of-band "latest answer" channel bypassing `interrupt()`/`resume`**:
  would reinvent state the checkpointer already gives for free, and breaks
  the "pause/resume survives a restart" guarantee the rest of this system
  is built around.

## New state: the active-project pointer

A single-line text file, `projects/.active_project_id`, holding the current
active project's id (absent/empty if none). Chosen over the SQLite-backed
project registry because it's live conversational pointer state, not
registration metadata — and not every project has a registry entry (ad hoc
`/new`-created projects never go through explicit registration). Matches
the existing "memory is boring and inspectable" pattern (README) — a human
can `cat` it. `OrchestratorService` derives its path from the existing
`projects_root` constructor argument; no new constructor parameter needed.

No locking: single-operator system (ADR-002), Telegram delivers one chat's
messages in order, and a whole-file overwrite is atomic enough for this
scope.

Written whenever a project is created (explicit-`project_id` path, or a
fresh random id), whenever free text resumes an active project's interview,
and whenever a decision or the new "Reply" activation updates it (see
below). Read once, only on the free-text path when `force_new` is `False`.

## Service layer (`service.py`)

`handle_webhook(self, project_id: str | None, text: str, force_new: bool = False)`:

- `project_id` given explicitly (the `registry/cli.py` path — unchanged
  caller, unchanged behavior): create-if-missing exactly as today, then
  write it as the active project.
- `project_id is None` and `force_new` is `False` (the Gateway's plain
  free-text path): read the active-project pointer. If that project exists
  and its `phase` isn't terminal:
  - `pending_approval["kind"] == "plan_questions"` → resume it via
    `Command(resume=text)` (the actual fix), then return its status.
  - Otherwise (mid-autonomous-execution, or paused at a button gate) →
    don't touch the graph at all; return its status with `text_applied`
    forced to `False`.
- Otherwise (no usable active project, active one is terminal/missing, or
  `force_new=True` — the Gateway's `/new` path): start a fresh project
  exactly as today, and record it as the new active project.

`resume()` (decision path) also writes the active-project pointer after a
successful resume — a button press is itself an explicit "I'm engaged with
project X" signal, consistent with the "most-recently-active" rule.

## API layer (`api.py`)

- `WebhookRequest` gains `force_new: bool = False`.
- `ProjectStatusResponse` gains `text_applied: bool = True`. Only the
  "active project isn't awaiting text" branch above ever sets it `False`;
  `/resume` and `/status` responses always carry the default, since neither
  endpoint has this concept — reusing the shared response model is the
  existing pattern (all three endpoints already return it).
- New endpoint: `POST /activate/{project_id}`. Sets the active-project
  pointer directly, no graph interaction. Backs the Gateway's new "Reply"
  button and is what `resume()` also calls internally.

## Gateway layer (`telegram_gateway/`)

- `rendering.render_approval` branches on `pending_approval["kind"]`:
  - `"plan_questions"` → the questions text plus a single **Reply** button
    (`callback_data="reply:<project_id>"`, using the payload's existing
    `thread_id`/`project_id` field, both already present at
    `graph/nodes.py:187-188`). No Approve/Reject/Edit — they don't map to
    anything meaningful for an interview answer, and this replaces the
    exact keyboard the user reported seeing incorrectly.
  - Every other kind: today's Approve/Reject/Edit keyboard, unchanged.
- New `bot.py` callback handler `handle_reply_callback`, triggered by the
  Reply button: calls `POST /activate/{project_id}` (via a new
  `OrchestratorClient.activate()` method), clears that message's keyboard
  (same pattern `handle_approval_callback` already uses), and sends a
  follow-up prompt with Telegram's `ForceReply` markup so the client opens
  the reply box automatically. This is the low-friction disambiguator for
  when 2+ projects are waiting at once — the common single-project case
  never needs it, since the active-project heuristic already gets it right
  without any button press.
- `OrchestratorClient.send_webhook` gains a `force_new: bool = False`
  parameter, threaded into the request body. `bot.py`'s `handle_new` calls
  it with `force_new=True`; `handle_free_text` leaves it `False`. This is
  the one explicit signal that distinguishes "/new" from a plain reply at
  the API boundary — today both are indistinguishable server-side, since
  neither ever sends `project_id`.
- `_relay_webhook_result` checks `body.get("text_applied")` first: if
  `False`, send an explicit hint (*"Project X is at phase Y and isn't
  waiting on typed replies right now -- use the buttons above, or
  /status."*) and return early — not a silent status echo, and not also
  re-rendering `pending_approval`'s keyboard (already visible higher up in
  the chat; resending it would just be noise).

## Testing

No new interrupt-contract tests needed: `plan_questions`/`planner_wait`
already exists and was already tested in Phase 8. This design only adds a
server-side path that correctly routes to the existing `Command(resume=
text)` call — it doesn't add a new pause point to the graph.

- `services/orchestrator/tests/unit/test_service.py`: `handle_webhook`
  resumes a `plan_questions`-paused active project via `Command(resume=
  text)`; returns `text_applied=False` without touching the graph when the
  active project is mid-execution or paused at a non-`plan_questions` gate;
  still starts fresh when no active project exists, when `force_new=True`,
  or when the active project is terminal/missing; explicit-`project_id`
  calls behave unchanged; `resume()` updates the active-project pointer.
- `services/telegram_gateway/tests/unit/test_rendering.py`: `plan_questions`
  kind renders a Reply-only keyboard; every other kind still renders
  Approve/Reject/Edit unchanged.
- `services/telegram_gateway/tests/unit/test_bot.py`: `handle_new` sends
  `force_new=True`; `handle_free_text` doesn't; `handle_reply_callback`
  calls `activate()` and sends a `ForceReply`-marked prompt;
  `_relay_webhook_result` shows the explicit hint and returns early when
  `text_applied` is `False`.
- `services/telegram_gateway/tests/unit/test_orchestrator_client.py`: new
  `activate()` method hits `POST /activate/{project_id}`; `send_webhook`
  threads `force_new` into the request body correctly.

Hand-traced only in this sandbox (permanent `langgraph`/`langchain`/
`aiogram`/`fastapi` gap since Phase 5) — real execution happens in CI/the
live deployment.
