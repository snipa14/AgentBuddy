# Design: Cancel/abort a project

Date: 2026-08-30
Status: approved

## Problem

A wrongly-started project (fat-fingered `/new`, wrong chat, changed your
mind) has no way to be gotten rid of. `/rename` can relabel it, but it
still shows up in `/projects`, still holds the active-project pointer if it
was just started, and a poller keeps relaying its progress. The only
"undo" today is ignoring it forever.

## Decision

Reuse `ProjectRecord.status` (`registry/schemas.py`'s `ProjectStatus =
Literal["active", "archived"]`, present since Phase 7, never wired to
anything) as the cancel flag. Cancelling a project sets its registry
`status` to `"archived"`; nothing about the LangGraph checkpoint changes.

Rejected alternative: writing a new terminal `phase` (e.g. `"cancelled"`)
directly into the graph state via `graph.update_state()`. That would make
cancellation a first-class phase alongside `done`/`blocked`, but touches
every `Phase`-Literal match site in the codebase, and risks racing a
genuinely in-flight `_graph.invoke()` call on the same thread if the
project isn't actually paused — a lost race would mean a "cancelled"
project silently un-cancels itself the moment the stale invoke finishes.
The registry-only approach has no such risk, since it never touches the
checkpoint, and is sufficient for the realistic case: a project someone
wants to cancel is almost always sitting at a `pending_approval` gate
(most often the intake interview's first `plan_questions` pause), not
mid-task.

## Revival rule

Rather than special-casing only `/continue`, any call that actually runs
the graph -- `OrchestratorService.resume()`, `.activate()`, or
`.handle_webhook()`'s explicit-`project_id` branch -- un-archives the
project first, via a new check at the top of the shared `_invoke_graph()`
entry point. `status()` (read-only) never revives anything. This keeps
"reviving" one uniform rule instead of three special cases, while still
matching the practical expectation that `/continue <cancelled-project>`
is what brings it back.

## Cancel target

`/cancel [project]`: omitted defaults to the currently active project
(mirrors `/rename`'s no-arg convention); an explicit id/name is also
accepted, unlike `/rename` -- cancelling something other than what's
active shouldn't require reactivating it first just to kill it.

No confirmation step. Cancelling is fully reversible via `/continue` and
never touches checkpoint state or any workspace/branch, so it doesn't
carry the destructive weight that would justify one.

## Surface changes

**`services/orchestrator/service.py`**
- `cancel_project(identifier: str | None) -> dict`: resolves `identifier`
  via the existing `_resolve_identifier()` (or the active pointer if
  `None`), raises `UnknownProjectError` if unresolvable, adopts an
  unregistered project into the registry first if needed (same pattern
  `rename_active_project()` already uses for a pre-registry-unification
  project), sets `status="archived"`, and -- only when the cancelled
  project is also the current active pointer -- clears the active pointer
  (written as an empty string, which `_read_active_project_id()` already
  treats as "none"). Returns `status(project_id)`.
- `_invoke_graph()` gains a pre-step: if the target project's registry
  entry is `"archived"`, flip it back to `"active"` before invoking.
- `status()` and `list_projects()` gain a `cancelled: bool` field (True
  iff the registry entry's `status == "archived"`).

**`services/orchestrator/api.py`**
- `CancelRequest(project: str | None = None)`, `POST /cancel` ->
  `service.cancel_project(request.project)`, 404 via `UnknownProjectError`.
- `ProjectStatusResponse.cancelled: bool`, `ProjectSummary.cancelled: bool`.

**`services/orchestrator/registry/manage_cli.py`**
- `foundry cancel <id-or-name>`: registry-only, same shape as the existing
  `rename` subcommand (resolve, set `status="archived"`, save). Does not
  touch the active-pointer file -- see Known limitation below.

**`services/telegram_gateway/`**
- `bot.py`: new `/cancel [project]` command, calling the new
  `OrchestratorClient.cancel()` -> `POST /cancel`.
- `rendering.py`: `render_project_status`/`render_project_list`/
  `render_project_buttons` append/prefix a `(cancelled)` tag when the
  project's `cancelled` field is true (same style as the existing
  `(active)` marker from the prior active-project-signal change).

## Known limitation (accepted, not fixed by this design)

`foundry cancel` (CLI) doesn't clear the Orchestrator's active-pointer
file -- that file's path is private to `service.py`, and factoring it out
so both the CLI and the service can share it is more infrastructure than
this warrants right now. Consequence: if a project is cancelled via the
CLI *and* it happens to also be Telegram's current active project, a
stray free-text message would silently revive it via the revival rule
above, before the operator notices. This only matters when both
conditions hold at once (CLI-cancel of the Telegram-active project) --
accepted as a known gap; revisit if it actually causes confusion in
practice.

## Testing plan

- `service.py`: `cancel_project` with an explicit target, with no arg
  (defaults to active), against an unregistered/legacy project (gets
  adopted), against an unknown identifier (raises). Active-pointer
  clearing only when the cancelled project was active. `_invoke_graph`'s
  un-archive-on-revival via `resume()`, `activate()`, and
  `handle_webhook`'s explicit-id path. `status()`/`list_projects()`
  surfacing `cancelled`.
- `api.py`: `POST /cancel` with/without a target, 404 on unknown project.
- `manage_cli.py`: `cancel` subcommand -- registry updated, unknown
  project error, duplicate-name logic is untouched (cancel doesn't rename).
- `bot.py`/`rendering.py`: `/cancel` command with/without an argument,
  `(cancelled)` tag rendering in status/list/buttons.
