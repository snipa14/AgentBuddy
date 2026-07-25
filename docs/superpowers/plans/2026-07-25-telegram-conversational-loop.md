# Telegram Conversational Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make free-text Telegram replies resume a paused mid-interview project instead of silently starting a new one, using an "active project" pointer and the `pending_approval["kind"]` field every interrupt payload already carries.

**Architecture:** A single active-project pointer file (`projects/.active_project_id`) lets `OrchestratorService.handle_webhook` tell "this text answers the active project's interview" apart from "this should start something new," using `force_new` (set only by `/new`) and `pending_approval["kind"] == "plan_questions"` (set only by the interview). A new `POST /activate/{project_id}` endpoint plus a Telegram "Reply" button (using `ForceReply`) let the operator explicitly disambiguate which project a reply targets when more than one is paused at once. No LangGraph/interrupt changes — `node_planner_ask`/`node_planner_wait` already work correctly; this plan only adds the server-side routing that was missing. Spec: `docs/superpowers/specs/2026-07-25-telegram-conversational-loop-design.md`.

**Tech Stack:** Python 3.12, FastAPI, aiogram 3, LangGraph (`Command(resume=...)`), pytest.

## Global Constraints

- Python 3.12; ruff `line-length = 100`; imports sorted (ruff `I`).
- **No inline comments** (CLAUDE.md) — docstrings only; single-line `#` only for non-obvious workarounds with an issue reference.
- Google-style docstrings on every public function and class.
- All commits go **directly on `release/v1`** (no feature branch/worktree — established repo workflow). Conventional Commits format.
- **Sandbox gap (permanent, expected):** `langgraph`, `langchain_core`, `fastapi`, `aiogram`, `httpx`, `pyyaml`, `pytest`, `ruff` may or may not all be importable in this sandbox (varies by service — `fastapi`/`httpx` have been importable in past sessions, `langgraph`/`langchain_core` have not). Attempt every `pytest` command once; if it fails on a missing import, hand-trace instead, state that clearly in the report and commit message, and do not attempt `pip install` (blocked by `block_host_installs.py`).
- The existing `fast_track_router`/`start_only_router`/`fast_track_router_with_curation` test fakes (`graph/tests/unit/planner_fakes.py`) consume their scripted LLM responses **in strict order, one response per LLM call** — a project that never resumes past `plan_wait` consumes only its first 3 (Planner-shaped) items, leaving the rest for a *different* project's calls if reused carelessly. This exact bug class has bitten this repo twice before (see `.superpowers/sdd/progress.md`). When a task below scripts a custom response list, the response count and order in the task text is exact and must be followed verbatim, not approximated.

---

### Task 1: Service layer — active-project pointer, `handle_webhook`/`resume`/`activate`

**Files:**
- Modify: `services/orchestrator/service.py`
- Test: `services/orchestrator/tests/unit/test_service.py`

**Interfaces:**
- Consumes: `OrchestratorService.__init__`'s existing `projects_root: Path` constructor argument (currently discarded after being passed to `build_project_graph` — must also be stored as `self._projects_root`); existing `self._project_exists(project_id) -> bool`, `self._config(project_id) -> dict`, `_TERMINAL_PHASES` module constant; `Command` from `langgraph.types` (already imported).
- Produces: `OrchestratorService.handle_webhook(self, project_id: str | None, text: str, force_new: bool = False) -> dict[str, Any]` (new `force_new` parameter, existing signature otherwise unchanged); `OrchestratorService.activate(self, project_id: str) -> None` (new public method, raises `UnknownProjectError` if unknown); the returned status dict from `handle_webhook` gains a `text_applied: bool` key **only** in the "active project isn't awaiting text" branch (value `False`); every other branch's returned dict has no `text_applied` key at all (Task 2's `ProjectStatusResponse.text_applied: bool = True` default fills it in at the API layer).

- [ ] **Step 1: Write the failing tests**

Add these imports to the top of `services/orchestrator/tests/unit/test_service.py`, alongside the existing ones:

```python
from services.orchestrator.agents.planner import InterviewTurn
from services.orchestrator.graph.tests.unit.planner_fakes import (
    CODER_DONE,
    CURATION_OK,
    EMPTY_DRAFT,
    FakeRouter,
    NO_FINDINGS_CRITIQUE,
    ONE_TASK,
    READY_TURN,
    REVIEWER_APPROVE,
    fast_track_router_with_curation,
    start_only_router,
    structured,
)
```

Append these tests to the end of the file:

```python
def test_handle_webhook_resumes_a_plan_questions_paused_active_project_with_free_text(
    tmp_path,
):
    """The core fix: a project paused mid-interview (pending_approval["kind"]
    == "plan_questions") must have free text routed to Command(resume=text),
    not treated as a new project's initial message.

    Response order: (1) InterviewTurn(status="questions") -- node_planner_ask's
    first call, pauses at planner_wait; (2) READY_TURN -- node_planner_ask's
    second call after resume, signals interview complete; (3) EMPTY_DRAFT --
    node_planner_draft; (4) NO_FINDINGS_CRITIQUE -- node_planner_critique,
    pauses at plan_wait. Exactly these 4, in this order, per one project that
    never reaches plan_wait's own resume in this test.
    """
    checkpointer = build_sqlite_checkpointer(tmp_path / "foundry_memory.db")
    memory_conn = build_global_memory_connection(tmp_path / "foundry_memory.db")
    responses = [
        structured(InterviewTurn(status="questions", questions=["Which language?"])),
        READY_TURN,
        EMPTY_DRAFT,
        NO_FINDINGS_CRITIQUE,
    ]
    service = OrchestratorService(
        checkpointer,
        FakeRouter(responses),
        _StubExecutorClient(),
        memory_conn,
        tmp_path / "projects",
        StacksConfig(stacks={}),
    )

    started = service.handle_webhook(None, "build me a thing", force_new=True)
    assert started["pending_approval"]["kind"] == "plan_questions"

    resumed = service.handle_webhook(None, "Python please")

    assert resumed["project_id"] == started["project_id"]
    assert resumed["phase"] == "awaiting_plan_approval"
    assert "text_applied" not in resumed


def test_handle_webhook_does_not_touch_the_graph_when_active_project_is_mid_execution(
    tmp_path,
):
    checkpointer = build_sqlite_checkpointer(tmp_path / "foundry_memory.db")
    memory_conn = build_global_memory_connection(tmp_path / "foundry_memory.db")
    service = OrchestratorService(
        checkpointer,
        fast_track_router_with_curation(rounds=1),
        _StubExecutorClient(),
        memory_conn,
        tmp_path / "projects",
        StacksConfig(stacks={}),
    )
    service.handle_webhook("my-project", "hello")
    service.resume("my-project", "approve")

    result = service.handle_webhook(None, "some unrelated text")

    assert result["project_id"] == "my-project"
    assert result["phase"] == "awaiting_merge_approval"
    assert result["text_applied"] is False


def test_handle_webhook_force_new_starts_a_fresh_project_even_with_an_active_one(tmp_path):
    checkpointer = build_sqlite_checkpointer(tmp_path / "foundry_memory.db")
    memory_conn = build_global_memory_connection(tmp_path / "foundry_memory.db")
    service = OrchestratorService(
        checkpointer,
        start_only_router(projects=2),
        _StubExecutorClient(),
        memory_conn,
        tmp_path / "projects",
        StacksConfig(stacks={}),
    )
    service.handle_webhook("my-project", "hello")

    result = service.handle_webhook(None, "brand new idea", force_new=True)

    assert result["project_id"] != "my-project"
    assert result["phase"] == "awaiting_plan_approval"


def test_handle_webhook_starts_fresh_when_active_project_is_terminal(tmp_path):
    """Response order: 9 items driving "my-project" all the way to done
    (READY_TURN/EMPTY_DRAFT/NO_FINDINGS_CRITIQUE/CURATION_OK for the plan
    interview+curator checkpoint, ONE_TASK/CODER_DONE/REVIEWER_APPROVE for
    the one task, CURATION_OK x2 for the testing and merge_wait curator
    checkpoints -- mirrors fast_track_router_with_curation(rounds=1) exactly),
    then 3 more (READY_TURN/EMPTY_DRAFT/NO_FINDINGS_CRITIQUE) for the second
    project's interview, which must NOT reuse "my-project"'s already-consumed
    responses.
    """
    checkpointer = build_sqlite_checkpointer(tmp_path / "foundry_memory.db")
    memory_conn = build_global_memory_connection(tmp_path / "foundry_memory.db")
    responses = [
        READY_TURN,
        EMPTY_DRAFT,
        NO_FINDINGS_CRITIQUE,
        CURATION_OK,
        ONE_TASK,
        CODER_DONE,
        REVIEWER_APPROVE,
        CURATION_OK,
        CURATION_OK,
        READY_TURN,
        EMPTY_DRAFT,
        NO_FINDINGS_CRITIQUE,
    ]
    service = OrchestratorService(
        checkpointer,
        FakeRouter(responses),
        _StubExecutorClient(),
        memory_conn,
        tmp_path / "projects",
        StacksConfig(stacks={}),
    )
    service.handle_webhook("my-project", "hello")
    service.resume("my-project", "approve")
    service.resume("my-project", "approve")
    assert service.status("my-project")["phase"] == "done"

    result = service.handle_webhook(None, "another idea")

    assert result["project_id"] != "my-project"
    assert result["phase"] == "awaiting_plan_approval"


def test_handle_webhook_starts_fresh_on_first_ever_message_with_no_active_project(tmp_path):
    checkpointer = build_sqlite_checkpointer(tmp_path / "foundry_memory.db")
    memory_conn = build_global_memory_connection(tmp_path / "foundry_memory.db")
    service = OrchestratorService(
        checkpointer,
        start_only_router(projects=1),
        _StubExecutorClient(),
        memory_conn,
        tmp_path / "projects",
        StacksConfig(stacks={}),
    )

    result = service.handle_webhook(None, "build me a thing")

    assert result["project_id"]
    assert result["phase"] == "awaiting_plan_approval"


def test_activate_marks_a_project_active_for_subsequent_free_text(tmp_path):
    """Response order: two independent projects' interview sequences
    (start_only_router(projects=2)'s own documented shape) -- project-a is
    created first (becoming active by that side effect), project-b second;
    explicitly re-activating project-a must make a later bare free-text
    webhook target project-a again, not project-b."""
    checkpointer = build_sqlite_checkpointer(tmp_path / "foundry_memory.db")
    memory_conn = build_global_memory_connection(tmp_path / "foundry_memory.db")
    service = OrchestratorService(
        checkpointer,
        start_only_router(projects=2),
        _StubExecutorClient(),
        memory_conn,
        tmp_path / "projects",
        StacksConfig(stacks={}),
    )
    service.handle_webhook("project-a", "hello")
    service.handle_webhook("project-b", "hello")

    service.activate("project-a")
    result = service.handle_webhook(None, "some text")

    assert result["project_id"] == "project-a"
    assert result["text_applied"] is False


def test_activate_unknown_project_raises(tmp_path):
    checkpointer = build_sqlite_checkpointer(tmp_path / "foundry_memory.db")
    memory_conn = build_global_memory_connection(tmp_path / "foundry_memory.db")
    service = OrchestratorService(
        checkpointer,
        FakeRouter([]),
        _StubExecutorClient(),
        memory_conn,
        tmp_path / "projects",
        StacksConfig(stacks={}),
    )

    with pytest.raises(UnknownProjectError):
        service.activate("does-not-exist")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest services/orchestrator/tests/unit/test_service.py -v`
Expected: FAIL — `TypeError: handle_webhook() got an unexpected keyword argument 'force_new'` (and `AttributeError: 'OrchestratorService' object has no attribute 'activate'` for the two `activate` tests).
Sandbox note: if `langgraph`/`fastapi` aren't importable here, this fails at collection instead (`ModuleNotFoundError`) — hand-trace: confirm `force_new` and `activate` don't exist yet in `service.py`.

- [ ] **Step 3: Implement the service-layer changes**

In `services/orchestrator/service.py`, add `self._projects_root = projects_root` to `__init__`, right after the existing `self._checkpointer = checkpointer` line (before `self._graph = build_project_graph(...)`, order doesn't matter but keep it near the other `self._` assignments).

Add this module-level constant near `_TERMINAL_PHASES`:

```python
_ACTIVE_PROJECT_FILENAME = ".active_project_id"
```

Add these three private methods anywhere in the `OrchestratorService` class (e.g. right after `_config`):

```python
    def _active_project_path(self) -> Path:
        return self._projects_root / _ACTIVE_PROJECT_FILENAME

    def _read_active_project_id(self) -> str | None:
        """Reads the active-project pointer.

        Returns:
            The active project's id, or None if no pointer file exists yet
            (a fresh deployment, or one where no project has ever been
            created or resumed).
        """
        path = self._active_project_path()
        if not path.exists():
            return None
        content = path.read_text().strip()
        return content or None

    def _write_active_project_id(self, project_id: str) -> None:
        """Overwrites the active-project pointer with project_id.

        No locking: this is a single-operator system (ADR-002) with
        Telegram delivering one chat's messages in order, and a whole-file
        overwrite is atomic enough for this scope.
        """
        path = self._active_project_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(project_id)
```

Replace `handle_webhook`'s full body and signature (keep its existing docstring's `Args`/`Returns`/`Raises` structure, extending it as shown):

```python
    def handle_webhook(
        self, project_id: str | None, text: str, force_new: bool = False
    ) -> dict[str, Any]:
        """Starts a new project thread, resumes an active interview, or
        echoes status for an existing one.

        When project_id is None and force_new is False, the active-project
        pointer (ADR: 2026-07-25 Telegram conversational loop) decides what
        happens: a live, non-terminal active project paused mid-interview
        (pending_approval["kind"] == "plan_questions") has text fed in via
        Command(resume=text); a live, non-terminal active project waiting on
        anything else (mid-execution, or paused at a button gate) is left
        untouched and its status is returned with text_applied forced to
        False, so the text is never silently dropped without a signal.

        Args:
            project_id: The project this message belongs to, or None to let
                the active-project pointer (or force_new) decide.
            text: The free-text message body. Seeds a new project's intake
                interview, or answers the active project's current
                clarifying-question round.
            force_new: If True, always start a fresh project, ignoring any
                active project. Set by the Gateway's /new handler.

        Returns:
            The resulting project's status dict (see `status()`), with a
            `text_applied: False` key added only when text was received but
            not applied to any project.

        Raises:
            InvalidProjectIdError: If a caller-supplied project_id for a new
                project isn't a safe, single path-segment slug. The registry
                validates IDs it creates, but this entrypoint accepts
                arbitrary caller input that later becomes a filesystem path
                component (the Memory Curator's `projects/<id>/memory/`
                writes), so it must apply the same allow-list itself.
        """
        if project_id is not None:
            if not self._project_exists(project_id):
                validate_project_id(project_id)
                initial_state = new_project_state(project_id, initial_message=text)
                self._graph.invoke(initial_state, config=self._config(project_id))
            self._write_active_project_id(project_id)
            return self.status(project_id)

        if not force_new:
            active_id = self._read_active_project_id()
            if active_id is not None and self._project_exists(active_id):
                active_status = self.status(active_id)
                if active_status["phase"] not in _TERMINAL_PHASES:
                    pending = active_status["pending_approval"]
                    if pending is not None and pending.get("kind") == "plan_questions":
                        self._graph.invoke(
                            Command(resume=text), config=self._config(active_id)
                        )
                        self._write_active_project_id(active_id)
                        return self.status(active_id)
                    return {**active_status, "text_applied": False}

        new_id = uuid.uuid4().hex[:12]
        initial_state = new_project_state(new_id, initial_message=text)
        self._graph.invoke(initial_state, config=self._config(new_id))
        self._write_active_project_id(new_id)
        return self.status(new_id)
```

Add `self._write_active_project_id(project_id)` to `resume()`, right after its existing `_project_exists` check and before the `self._graph.invoke(...)` line:

```python
    def resume(self, project_id: str, decision: str) -> dict[str, Any]:
        if not self._project_exists(project_id):
            raise UnknownProjectError(project_id)
        self._write_active_project_id(project_id)
        self._graph.invoke(Command(resume=decision), config=self._config(project_id))
        return self.status(project_id)
```

(Keep `resume()`'s existing docstring unchanged — its behavior from the caller's perspective is unaffected.)

Add a new public method, placed after `resume()` and before `status()`:

```python
    def activate(self, project_id: str) -> None:
        """Marks project_id as the active project for future bare free text.

        Args:
            project_id: The project to activate.

        Raises:
            UnknownProjectError: If project_id has no checkpoint history.
        """
        if not self._project_exists(project_id):
            raise UnknownProjectError(project_id)
        self._write_active_project_id(project_id)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest services/orchestrator/tests/unit/test_service.py -v`
Expected: PASS — all new tests plus every pre-existing test in this file (in particular `test_handle_webhook_without_project_id_starts_a_new_project`, `test_handle_webhook_with_explicit_project_id_starts_that_project`, `test_handle_webhook_is_idempotent_status_echo_for_known_project`, `test_resume_known_project_progresses_through_merge_gate_to_done`, `test_list_projects_returns_known_ids` — none of these pass `force_new` or rely on the active-project pointer, so their behavior must be byte-identical to before).
Sandbox note: hand-trace every test above line by line against the new `handle_webhook`/`resume`/`activate` bodies if `langgraph`/`fastapi` aren't importable here.

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/service.py services/orchestrator/tests/unit/test_service.py
git commit -m "feat(orchestrator): resume the active project's interview from free text"
```

---

### Task 2: API layer — `force_new`, `text_applied`, `POST /activate/{project_id}`

**Files:**
- Modify: `services/orchestrator/api.py`
- Test: `services/orchestrator/tests/unit/test_api.py`

**Interfaces:**
- Consumes: `OrchestratorService.handle_webhook(project_id, text, force_new=False)` and `OrchestratorService.activate(project_id)` from Task 1; existing `UnknownProjectError`, `InvalidProjectIdError`.
- Produces: `WebhookRequest.force_new: bool = False`; `ProjectStatusResponse.text_applied: bool = True`; `POST /activate/{project_id}` returning `ProjectStatusResponse` (200) or 404 on `UnknownProjectError`.

- [ ] **Step 1: Write the failing tests**

Append to `services/orchestrator/tests/unit/test_api.py`:

```python
def test_webhook_force_new_starts_a_fresh_project_even_with_an_active_one(client):
    client.post("/webhook", json={"user_id": 1, "text": "hi", "project_id": "my-project"})

    response = client.post(
        "/webhook", json={"user_id": 1, "text": "brand new idea", "force_new": True}
    )

    assert response.json()["project_id"] != "my-project"


def test_webhook_response_defaults_text_applied_to_true(client):
    response = client.post(
        "/webhook", json={"user_id": 1, "text": "hi", "project_id": "my-project"}
    )

    assert response.json()["text_applied"] is True


def test_activate_unknown_project_returns_404(client):
    response = client.post("/activate/does-not-exist")

    assert response.status_code == 404


def test_activate_known_project_returns_its_status(client):
    client.post("/webhook", json={"user_id": 1, "text": "hi", "project_id": "my-project"})

    response = client.post("/activate/my-project")

    assert response.status_code == 200
    assert response.json()["project_id"] == "my-project"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest services/orchestrator/tests/unit/test_api.py -v`
Expected: FAIL — `force_new` rejected as an unexpected field is NOT expected (Pydantic ignores unknown fields by default, so that alone won't fail) but `test_webhook_response_defaults_text_applied_to_true` fails with `KeyError: 'text_applied'`, and both `/activate/...` tests fail with 404 (route not found) since the endpoint doesn't exist yet.
Sandbox note: hand-trace if `fastapi` isn't importable here.

- [ ] **Step 3: Implement the API changes**

In `services/orchestrator/api.py`, modify `WebhookRequest`:

```python
class WebhookRequest(BaseModel):
    """Payload the Telegram Gateway forwards for every command or free-text message."""

    user_id: int
    text: str
    project_id: str | None = None
    force_new: bool = False
```

Modify `ProjectStatusResponse`:

```python
class ProjectStatusResponse(BaseModel):
    """A project's current phase and any pending approval, rendered by the Gateway."""

    project_id: str
    phase: str
    pending_approval: dict | None
    task_progress: dict | None
    text_applied: bool = True
```

Update the `webhook` route handler to pass `force_new` through:

```python
    @app.post("/webhook", response_model=ProjectStatusResponse)
    def webhook(request: WebhookRequest) -> ProjectStatusResponse:
        try:
            result = service.handle_webhook(
                request.project_id, request.text, force_new=request.force_new
            )
        except InvalidProjectIdError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return ProjectStatusResponse(**result)
```

Add a new route, placed after `resume` and before `stream`:

```python
    @app.post("/activate/{project_id}", response_model=ProjectStatusResponse)
    def activate(project_id: str) -> ProjectStatusResponse:
        try:
            service.activate(project_id)
        except UnknownProjectError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return ProjectStatusResponse(**service.status(project_id))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest services/orchestrator/tests/unit/test_api.py -v`
Expected: PASS — all new tests plus every pre-existing test in this file.
Sandbox note: hand-trace if `fastapi` isn't importable here.

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/api.py services/orchestrator/tests/unit/test_api.py
git commit -m "feat(orchestrator): expose force_new, text_applied, and POST /activate"
```

---

### Task 3: Gateway rendering — Reply-only keyboard for interview questions

**Files:**
- Modify: `services/telegram_gateway/rendering.py`
- Test: `services/telegram_gateway/tests/unit/test_rendering.py`

**Interfaces:**
- Consumes: nothing from Tasks 1-2 (this task only reshapes what `render_approval` returns based on the `pending_approval` dict it already receives — the `"kind"` field it reads has existed in every interrupt payload since before this plan).
- Produces: `CALLBACK_REPLY = "reply"` module constant, exported alongside the existing `CALLBACK_APPROVE`/`CALLBACK_REJECT`/`CALLBACK_EDIT`; `render_approval` returns a Reply-only keyboard when `pending_approval.get("kind") == "plan_questions"`, unchanged Approve/Reject/Edit otherwise. Task 4's `bot.py` imports `CALLBACK_REPLY` from this module.

- [ ] **Step 1: Write the failing tests**

Append to `services/telegram_gateway/tests/unit/test_rendering.py`, and add `CALLBACK_REPLY` to the existing `from services.telegram_gateway.rendering import (...)` import line:

```python
def test_render_approval_shows_only_a_reply_button_for_plan_questions():
    text, keyboard = render_approval(
        {
            "thread_id": "proj-1",
            "kind": "plan_questions",
            "summary": "- Which language?\n- Which framework?",
        }
    )

    assert text == "- Which language?\n- Which framework?"
    buttons = keyboard.inline_keyboard[0]
    assert [button.text for button in buttons] == ["Reply"]
    assert [button.callback_data for button in buttons] == ["reply:proj-1"]


def test_render_approval_shows_approve_reject_edit_for_every_other_kind():
    text, keyboard = render_approval(
        {"thread_id": "proj-1", "kind": "merge_approval", "summary": "Merge to main?"}
    )

    assert text == "Merge to main?"
    buttons = keyboard.inline_keyboard[0]
    assert [button.text for button in buttons] == ["Approve", "Reject", "Edit"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest services/telegram_gateway/tests/unit/test_rendering.py -v`
Expected: FAIL — `test_render_approval_shows_only_a_reply_button_for_plan_questions` fails because today's `render_approval` always returns the 3-button Approve/Reject/Edit keyboard regardless of `kind`.
Sandbox note: hand-trace if `aiogram` isn't importable here.

- [ ] **Step 3: Implement the rendering change**

In `services/telegram_gateway/rendering.py`, add the new constant next to the existing three:

```python
CALLBACK_APPROVE = "approve"
CALLBACK_REJECT = "reject"
CALLBACK_EDIT = "edit"
CALLBACK_REPLY = "reply"
```

Replace `render_approval`'s body:

```python
def render_approval(pending_approval: dict[str, Any]) -> tuple[str, InlineKeyboardMarkup]:
    """Renders a pending_approval payload as message text plus an inline keyboard.

    Args:
        pending_approval: The payload attached to a paused graph's
            interrupt() call (ARCHITECTURE.md §5), carrying at least a
            "thread_id" and, usually, a human-readable "summary".

    Returns:
        A (text, keyboard) pair ready to pass to a Message's answer/reply.
        A "plan_questions" kind (the Planner's clarifying-question
        interview) gets a single Reply button, since free text is how it's
        actually answered -- see docs/superpowers/specs/
        2026-07-25-telegram-conversational-loop-design.md. Every other kind
        keeps the Approve/Reject/Edit keyboard, whose callback_data encodes
        the decision and thread_id as "<decision>:<thread_id>".
    """
    thread_id = pending_approval["thread_id"]
    text = pending_approval.get("summary", _DEFAULT_TEXT)
    if pending_approval.get("kind") == "plan_questions":
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="Reply", callback_data=f"{CALLBACK_REPLY}:{thread_id}")]
            ]
        )
        return text, keyboard
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Approve", callback_data=f"{CALLBACK_APPROVE}:{thread_id}"
                ),
                InlineKeyboardButton(
                    text="Reject", callback_data=f"{CALLBACK_REJECT}:{thread_id}"
                ),
                InlineKeyboardButton(text="Edit", callback_data=f"{CALLBACK_EDIT}:{thread_id}"),
            ]
        ]
    )
    return text, keyboard
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest services/telegram_gateway/tests/unit/test_rendering.py -v`
Expected: PASS — both new tests, plus the pre-existing `test_render_approval_includes_summary_text_and_three_buttons` and `test_render_approval_falls_back_to_default_text_when_summary_missing` (neither passes a `"kind"` key, so `pending_approval.get("kind")` is `None`, which is `!= "plan_questions"`, so both still take the unchanged Approve/Reject/Edit branch).
Sandbox note: hand-trace if `aiogram` isn't importable here.

- [ ] **Step 5: Commit**

```bash
git add services/telegram_gateway/rendering.py services/telegram_gateway/tests/unit/test_rendering.py
git commit -m "feat(gateway): render a Reply-only keyboard for interview questions"
```

---

### Task 4: Gateway wiring — `force_new`, `activate()`, the Reply callback, and the `text_applied` hint

**Files:**
- Modify: `services/telegram_gateway/orchestrator_client.py`
- Modify: `services/telegram_gateway/bot.py`
- Test: `services/telegram_gateway/tests/unit/test_orchestrator_client.py`
- Test: `services/telegram_gateway/tests/unit/test_bot_handlers.py`

**Interfaces:**
- Consumes: `CALLBACK_REPLY` from Task 3's `rendering.py`; the Orchestrator's `POST /webhook` (now accepting `force_new`), `POST /activate/{project_id}`, and `text_applied` in every webhook/resume response (Task 2).
- Produces: `OrchestratorClient.send_webhook(self, user_id: int, text: str, force_new: bool = False) -> httpx.Response` (existing signature extended); `OrchestratorClient.activate(self, project_id: str) -> httpx.Response` (new); `bot.py`'s `handle_reply_callback` (new, registered for `CALLBACK_REPLY`-prefixed callback data); `_relay_webhook_result` returns early with an explicit hint when the response body's `text_applied` is `False`.

- [ ] **Step 1: Write the failing tests**

In `services/telegram_gateway/tests/unit/test_orchestrator_client.py`, replace `test_send_webhook_posts_user_id_and_text`:

```python
def test_send_webhook_posts_user_id_and_text():
    client = _mock_httpx_client()
    orchestrator = OrchestratorClient("http://orchestrator:8000", client)

    asyncio.run(orchestrator.send_webhook(42, "hello"))

    args, kwargs = client.post.call_args
    assert args[0] == "http://orchestrator:8000/webhook"
    assert kwargs["json"] == {"user_id": 42, "text": "hello", "force_new": False}


def test_send_webhook_can_force_a_new_project():
    client = _mock_httpx_client()
    orchestrator = OrchestratorClient("http://orchestrator:8000", client)

    asyncio.run(orchestrator.send_webhook(42, "hello", force_new=True))

    _, kwargs = client.post.call_args
    assert kwargs["json"] == {"user_id": 42, "text": "hello", "force_new": True}
```

Append to the same file:

```python
def test_activate_posts_to_the_project_specific_url():
    client = _mock_httpx_client()
    orchestrator = OrchestratorClient("http://orchestrator:8000/", client)

    asyncio.run(orchestrator.activate("proj-1"))

    args, _ = client.post.call_args
    assert args[0] == "http://orchestrator:8000/activate/proj-1"
```

In `services/telegram_gateway/tests/unit/test_bot_handlers.py`, update `test_handle_new_forwards_to_orchestrator_and_acknowledges`:

```python
def test_handle_new_forwards_to_orchestrator_and_acknowledges(message, orchestrator, poller):
    asyncio.run(handle_new(message, orchestrator, poller))

    orchestrator.send_webhook.assert_awaited_once_with(42, "/new build me a thing", force_new=True)
    message.answer.assert_awaited_once()
```

(`test_handle_free_text_forwards_to_orchestrator` at line 167-173 is unchanged — `handle_free_text` doesn't pass `force_new` explicitly, so the client method's own default applies and the existing 2-positional-arg assertion still matches.)

Append to `services/telegram_gateway/tests/unit/test_bot_handlers.py`:

```python
def test_handle_free_text_shows_a_hint_when_text_was_not_applied(message, orchestrator, poller):
    response = MagicMock()
    response.json.return_value = {
        "project_id": "proj-1",
        "phase": "awaiting_merge_approval",
        "pending_approval": {"thread_id": "proj-1", "summary": "Merge to main?"},
        "text_applied": False,
    }
    orchestrator.send_webhook = AsyncMock(return_value=response)

    asyncio.run(handle_free_text(message, orchestrator, poller))

    message.answer.assert_awaited_once_with(
        "Project proj-1 is at phase 'awaiting_merge_approval' and isn't waiting on typed "
        "replies right now -- use the buttons above, or /status."
    )


def test_handle_reply_callback_activates_the_project_and_prompts_for_a_reply(orchestrator):
    from aiogram.types import ForceReply

    orchestrator.activate = AsyncMock(return_value=_status_response(project_id="proj-1"))
    callback = _callback("reply:proj-1")

    asyncio.run(handle_reply_callback(callback, orchestrator))

    orchestrator.activate.assert_awaited_once_with("proj-1")
    callback.answer.assert_awaited_once()
    callback.message.edit_reply_markup.assert_awaited_once_with(reply_markup=None)
    args, kwargs = callback.message.answer.call_args
    assert isinstance(kwargs.get("reply_markup"), ForceReply)


def test_handle_reply_callback_handles_missing_message_gracefully(orchestrator):
    orchestrator.activate = AsyncMock(return_value=_status_response(project_id="proj-1"))
    callback = MagicMock()
    callback.data = "reply:proj-1"
    callback.answer = AsyncMock()
    callback.message = None

    asyncio.run(handle_reply_callback(callback, orchestrator))

    orchestrator.activate.assert_awaited_once_with("proj-1")
```

Add `handle_reply_callback` to the existing `from services.telegram_gateway.bot import (...)` import line in `test_bot_handlers.py`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest services/telegram_gateway/tests/unit/test_orchestrator_client.py services/telegram_gateway/tests/unit/test_bot_handlers.py -v`
Expected: FAIL — `send_webhook`'s payload comparisons fail (no `force_new` key yet), `activate` doesn't exist on `OrchestratorClient`, `handle_reply_callback` doesn't exist in `bot.py`, and the hint-message assertion fails against today's `_relay_webhook_result` (which doesn't check `text_applied`).
Sandbox note: hand-trace if `httpx`/`aiogram` aren't importable here.

- [ ] **Step 3: Implement the Gateway changes**

In `services/telegram_gateway/orchestrator_client.py`, replace `send_webhook`:

```python
    async def send_webhook(self, user_id: int, text: str, force_new: bool = False) -> httpx.Response:
        """Forwards a free-text message or command to POST /webhook."""
        return await self._client.post(
            f"{self._base_url}/webhook",
            json={"user_id": user_id, "text": text, "force_new": force_new},
        )
```

Add a new method, placed after `resume`:

```python
    async def activate(self, project_id: str) -> httpx.Response:
        """Marks project_id active via POST /activate/{project_id}."""
        return await self._client.post(f"{self._base_url}/activate/{project_id}")
```

In `services/telegram_gateway/bot.py`, add `ForceReply` to the existing `from aiogram.types import CallbackQuery, Message` import line (becomes `from aiogram.types import CallbackQuery, ForceReply, Message`), and add `CALLBACK_REPLY` to the existing `from services.telegram_gateway.rendering import (...)` import block.

Replace `_relay_webhook_result`:

```python
async def _relay_webhook_result(message: Message, response) -> None:
    """Renders a webhook response: an explicit hint if the text wasn't
    applied to any project, an Approve/Reject/Edit or Reply keyboard if the
    project is now paused at a gate, a task-progress summary if tasks
    exist, a plain acknowledgment otherwise.

    Args:
        message: The incoming message to reply to.
        response: The httpx.Response returned by OrchestratorClient.send_webhook.
    """
    body = response.json()
    if body.get("text_applied") is False:
        await message.answer(
            f"Project {body['project_id']} is at phase '{body['phase']}' and isn't "
            "waiting on typed replies right now -- use the buttons above, or /status."
        )
        return
    pending_approval = body.get("pending_approval")
    if pending_approval:
        text, keyboard = render_approval(pending_approval)
        await message.answer(text, reply_markup=keyboard)
        return
    task_progress = body.get("task_progress")
    if task_progress:
        await message.answer(render_task_progress(task_progress))
        return
    phase = body.get("phase")
    if phase in ("done", "blocked"):
        await message.answer(f"Project {body.get('project_id')} is {phase}.")
        return
    await message.answer("Got it -- working on that.")
```

Replace `handle_new`:

```python
@router.message(Command("new"))
async def handle_new(
    message: Message, orchestrator: OrchestratorClient, poller: ProgressPoller
) -> None:
    """Forwards a /new command to the Orchestrator to start a project."""
    response = await orchestrator.send_webhook(message.from_user.id, message.text, force_new=True)
    _start_poller_from_response(poller, response, message.chat.id)
    await _relay_webhook_result(message, response)
```

Add a new callback handler, placed after `handle_approval_callback`:

```python
@router.callback_query(F.data.startswith(CALLBACK_REPLY))
async def handle_reply_callback(callback: CallbackQuery, orchestrator: OrchestratorClient) -> None:
    """Activates the tapped project and opens a force-reply text box.

    The Reply button on an interview-question message is the disambiguator
    for when more than one project is paused at once (the common
    single-project case never needs it -- see docs/superpowers/specs/
    2026-07-25-telegram-conversational-loop-design.md).
    """
    _, project_id = callback.data.split(":", 1)
    await orchestrator.activate(project_id)
    await callback.answer()
    if callback.message is None:
        return
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(
        "Type your answer:", reply_markup=ForceReply(selective=True)
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest services/telegram_gateway/tests/unit/test_orchestrator_client.py services/telegram_gateway/tests/unit/test_bot_handlers.py -v`
Expected: PASS — all new and modified tests, plus every other pre-existing test in both files (in particular `test_handle_free_text_forwards_to_orchestrator`, `test_handle_new_starts_progress_polling_for_the_new_project`, `test_handle_new_renders_an_approval_keyboard_when_a_gate_is_pending`, `test_handle_approval_callback_*` — none of these set `text_applied` in their mocked responses, so `body.get("text_applied") is False` is `False` for all of them and they fall through to the unchanged rendering paths below it).
Sandbox note: hand-trace if `httpx`/`aiogram` aren't importable here.

- [ ] **Step 5: Commit**

```bash
git add services/telegram_gateway/orchestrator_client.py services/telegram_gateway/bot.py services/telegram_gateway/tests/unit/test_orchestrator_client.py services/telegram_gateway/tests/unit/test_bot_handlers.py
git commit -m "feat(gateway): wire force_new, the Reply button, and the text_applied hint"
```

---

## Final verification (after all tasks)

- [ ] Run: `pytest services/orchestrator services/telegram_gateway --cov=services/orchestrator --cov=services/telegram_gateway --cov-report=term-missing`
  Expected: PASS, coverage at or above this repo's 80% floor.
  Sandbox note: re-read the full diff (`git diff <task-1-base>..HEAD`) and hand-verify every test against its implementation if the real suite can't run here; state clearly which verifications were real runs vs. hand-traces.
- [ ] Run: `ruff check . && ruff format --check .`
  Sandbox note: visual check (line length, import order, no inline comments) if `ruff` isn't installed here.
- [ ] Manual smoke test on the real deployment (user-run): send `/new <description>` on Telegram, answer the first round of clarifying questions with plain text, confirm the Planner asks a *different* follow-up round (not the same questions again) or proceeds to plan drafting. With two projects going at once, confirm tapping a "Reply" button before answering routes text to the tapped project.
