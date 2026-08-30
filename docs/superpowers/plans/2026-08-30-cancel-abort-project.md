# Cancel/Abort a Project Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let an operator cancel a wrongly-started project (registry-only "archived" flag, fully reversible via `/continue`), surfaced across the Orchestrator API, the `foundry` CLI, and Telegram.

**Architecture:** Reuse the existing, currently-unused `ProjectRecord.status` field (`"active"`/`"archived"`) as the cancel flag -- no LangGraph checkpoint mutation, no new `Phase` value. `OrchestratorService.cancel_project()` flips it to `"archived"`; a new check inside `_write_active_project_id()` flips it back to `"active"` the moment the project becomes the active one again via `resume()`, `activate()`, or `handle_webhook`'s explicit-id branch, so `/continue` is a free "undo" with no dedicated un-cancel command.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, aiogram, pytest.

**Spec:** `docs/superpowers/specs/2026-08-30-cancel-abort-project-design.md`

## Global Constraints

- No LangGraph checkpoint state is ever touched by cancel/revive -- only `projects.yaml` (`ProjectRecord.status`) and the active-pointer file.
- Cancelling never requires a confirmation step (reversible, not destructive).
- `foundry cancel` (CLI) does NOT clear the Orchestrator's active-pointer file -- accepted limitation per spec, do not "fix" this as part of this plan.
- Every new/changed field defaults such that a project with no registry entry is treated as `cancelled: False` (never crashes on a missing record).

---

## Task 1: `status()`/`list_projects()` expose `cancelled`, via a shared `_lookup_record()` helper

**Files:**
- Modify: `services/orchestrator/service.py`
- Test: `services/orchestrator/tests/unit/test_service.py`

**Interfaces:**
- Consumes: `services.orchestrator.registry.store.load_registry`, `services.orchestrator.registry.schemas.ProjectRecord` (already imported in `service.py`).
- Produces: `OrchestratorService._lookup_record(project_id: str) -> ProjectRecord | None` (replaces `_lookup_name`). `status()`'s returned dict gains `"cancelled": bool`. `list_projects()`'s per-entry dicts gain `"cancelled": bool`.

- [ ] **Step 1: Write the failing tests**

Add to `services/orchestrator/tests/unit/test_service.py`, right after `test_status_unregistered_project_has_no_name` (search for that function to place these near it):

```python
def test_status_unregistered_project_is_not_cancelled(service):
    service.handle_webhook("my-project", "hello")

    result = service.status("my-project")

    assert result["cancelled"] is False


def test_status_registered_active_project_is_not_cancelled(tmp_path):
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
    service.handle_webhook(None, "/new Build me a CLI", force_new=True)

    result = service.status("build-me-a-cli")

    assert result["cancelled"] is False


def test_list_projects_includes_cancelled_field(tmp_path):
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
    service.handle_webhook("my-project", "hello")

    result = service.list_projects()

    assert result == [{"id": "my-project", "name": None, "active": True, "cancelled": False}]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest services/orchestrator/tests/unit/test_service.py -k "cancelled" -v`
Expected: FAIL with `KeyError: 'cancelled'`

- [ ] **Step 3: Implement `_lookup_record()` and wire `cancelled` through**

In `services/orchestrator/service.py`, replace the existing `_lookup_name` method (it currently reads:

```python
    def _lookup_name(self, project_id: str) -> str | None:
        for record in load_registry(self._registry_path):
            if record.id == project_id:
                return record.name
        return None
```

) with:

```python
    def _lookup_record(self, project_id: str) -> ProjectRecord | None:
        for record in load_registry(self._registry_path):
            if record.id == project_id:
                return record
        return None
```

Then update `status()`'s return block. It currently reads:

```python
        return {
            "project_id": project_id,
            "name": self._lookup_name(project_id),
            "active": self._read_active_project_id() == project_id,
            "phase": phase,
            "pending_approval": snapshot.values["pending_approval"],
            "task_progress": task_progress,
        }
```

Replace with:

```python
        record = self._lookup_record(project_id)
        return {
            "project_id": project_id,
            "name": record.name if record else None,
            "active": self._read_active_project_id() == project_id,
            "cancelled": record.status == "archived" if record else False,
            "phase": phase,
            "pending_approval": snapshot.values["pending_approval"],
            "task_progress": task_progress,
        }
```

Also update `status()`'s docstring `Returns:` block to add a line: `` `cancelled` (True if the project has been archived via `cancel_project()`), `` right after the `active` line.

Then update `list_projects()`. It currently reads:

```python
        names = {record.id: record.name for record in load_registry(self._registry_path)}
        active_id = self._read_active_project_id()
        return [
            {"id": project_id, "name": names.get(project_id), "active": project_id == active_id}
            for project_id in sorted(self._checkpoint_thread_ids())
        ]
```

Replace with:

```python
        records = {record.id: record for record in load_registry(self._registry_path)}
        active_id = self._read_active_project_id()
        return [
            {
                "id": project_id,
                "name": records[project_id].name if project_id in records else None,
                "active": project_id == active_id,
                "cancelled": (
                    records[project_id].status == "archived" if project_id in records else False
                ),
            }
            for project_id in sorted(self._checkpoint_thread_ids())
        ]
```

Also update `list_projects()`'s docstring `Returns:` line to mention the new `cancelled` key, matching the style of the existing `active` mention.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest services/orchestrator/tests/unit/test_service.py -v`
Expected: PASS (all tests in the file, not just the new ones -- confirms the `_lookup_name` -> `_lookup_record` rename didn't break anything)

- [ ] **Step 5: Lint and commit**

```bash
ruff check services/orchestrator/service.py services/orchestrator/tests/unit/test_service.py
ruff format services/orchestrator/service.py services/orchestrator/tests/unit/test_service.py
git add services/orchestrator/service.py services/orchestrator/tests/unit/test_service.py
git commit -m "feat(orchestrator): surface cancelled status on status()/list_projects()"
```

---

## Task 2: Revival rule -- `_write_active_project_id()` un-archives before writing

**Design correction from the spec's original framing:** the spec/initial
design said "hook the revival check into `_invoke_graph()`". That's wrong
for the most common real case: a project cancelled while paused at a
`pending_approval` gate. `activate()` only calls `_invoke_graph()` when the
project is genuinely *stuck* (no `pending_approval`); for a project sitting
at a normal gate (the realistic "I cancelled this while it was waiting on
me" case), `/continue` just rewrites the active pointer and returns --
`_invoke_graph()` never runs. `handle_webhook()`'s explicit-id branch has
the same gap for an already-existing project. `_write_active_project_id()`,
by contrast, is called by every one of `resume()`, `activate()`, and
`handle_webhook()`'s explicit-id branch, unconditionally -- becoming the
active project is what should mean "the human is engaging with this
again," not "the graph happened to run."

**Files:**
- Modify: `services/orchestrator/service.py`
- Test: `services/orchestrator/tests/unit/test_service.py`

**Interfaces:**
- Consumes: `ProjectRecord`, `load_registry`, `save_registry` (already imported). `Task 1`'s `_lookup_record()` is not reused here (this needs the full record list to write back, not just one record).
- Produces: `OrchestratorService._unarchive_if_needed(project_id: str) -> None`, called from `_write_active_project_id()`.

- [ ] **Step 1: Write the failing tests**

Add to `services/orchestrator/tests/unit/test_service.py`, near the other `resume`/`activate` tests (e.g. after `test_resume_known_project_progresses_through_merge_gate_to_done`):

```python
def test_resume_revives_a_cancelled_project(service):
    service.handle_webhook("my-project", "hello")
    save_registry(
        service._registry_path,
        [
            ProjectRecord(
                id="my-project",
                name="My Project",
                mode="greenfield",
                path=str(service._projects_root / "my-project"),
                stack="unspecified",
                status="archived",
            )
        ],
    )

    result = service.resume("my-project", "approve")

    assert result["cancelled"] is False
```

Also add, right after it -- this is the important case: a project paused at
a real gate (`pending_approval` is not None), where `activate()` does
*not* call `_invoke_graph()` at all, so the revival must happen purely from
`_write_active_project_id()`:

```python
def test_activate_revives_a_cancelled_project_paused_at_a_gate(tmp_path):
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
    service.handle_webhook("proj-1", "build me a thing")
    status_before = service.status("proj-1")
    assert status_before["pending_approval"] is not None
    save_registry(
        service._registry_path,
        [
            ProjectRecord(
                id="proj-1",
                name="Proj 1",
                mode="greenfield",
                path=str(tmp_path / "projects" / "proj-1"),
                stack="unspecified",
                status="archived",
            )
        ],
    )

    service.activate("proj-1")

    assert service.status("proj-1")["cancelled"] is False
```

Add the needed imports at the top of `services/orchestrator/tests/unit/test_service.py` -- it currently has:

```python
from services.orchestrator.registry.errors import DuplicateNameError
from services.orchestrator.registry.store import load_registry
```

Change the second line to also import `save_registry` and add a `schemas` import:

```python
from services.orchestrator.registry.errors import DuplicateNameError
from services.orchestrator.registry.schemas import ProjectRecord
from services.orchestrator.registry.store import load_registry, save_registry
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest services/orchestrator/tests/unit/test_service.py -k "revives_a_cancelled" -v`
Expected: FAIL with `assert True is False` (nothing un-archives it yet)

- [ ] **Step 3: Implement `_unarchive_if_needed()` and wire it into `_write_active_project_id()`**

In `services/orchestrator/service.py`, add a new method right after `_read_active_project_id` (which currently ends with `return content or None`):

```python
    def _unarchive_if_needed(self, project_id: str) -> None:
        """Un-cancels project_id if its registry entry is currently archived.

        Args:
            project_id: The project to check. A falsy value -- e.g. the
                empty string `cancel_project()` writes to *clear* the
                active pointer -- is always a no-op, never treated as a
                real project id to look up.
        """
        if not project_id:
            return
        records = load_registry(self._registry_path)
        for index, record in enumerate(records):
            if record.id == project_id and record.status == "archived":
                records[index] = record.model_copy(update={"status": "active"})
                save_registry(self._registry_path, records)
                return
```

Then update `_write_active_project_id` itself. It currently reads:

```python
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

Replace with:

```python
    def _write_active_project_id(self, project_id: str) -> None:
        """Overwrites the active-project pointer with project_id.

        Also revives project_id if it's currently cancelled (registry
        `status` is `"archived"`) -- every caller of this method represents
        the human engaging with project_id again (resuming a gate,
        continuing a stuck project, replying to its interview), so becoming
        the active project is what un-cancels it. `status()` never calls
        this method, so merely viewing a cancelled project never revives it.

        No locking: this is a single-operator system (ADR-002) with
        Telegram delivering one chat's messages in order, and a whole-file
        overwrite is atomic enough for this scope.
        """
        self._unarchive_if_needed(project_id)
        path = self._active_project_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(project_id)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest services/orchestrator/tests/unit/test_service.py -v`
Expected: PASS (full file)

- [ ] **Step 5: Lint and commit**

```bash
ruff check services/orchestrator/service.py services/orchestrator/tests/unit/test_service.py
ruff format services/orchestrator/service.py services/orchestrator/tests/unit/test_service.py
git add services/orchestrator/service.py services/orchestrator/tests/unit/test_service.py
git commit -m "feat(orchestrator): revive a cancelled project on resume/activate"
```

---

## Task 3: `cancel_project()`

**Files:**
- Modify: `services/orchestrator/service.py`
- Test: `services/orchestrator/tests/unit/test_service.py`

**Interfaces:**
- Consumes: `_resolve_identifier`, `_read_active_project_id`, `_write_active_project_id`, `_project_exists`, `UnknownProjectError`, `ProjectRecord`, `load_registry`, `save_registry` (all already present/imported).
- Produces: `OrchestratorService.cancel_project(identifier: str | None) -> dict[str, Any]`.

- [ ] **Step 1: Write the failing tests**

Add to `services/orchestrator/tests/unit/test_service.py`, near the `rename_active_project` tests (after `test_rename_active_project_adopts_a_project_with_no_registry_entry`):

```python
def test_cancel_project_by_explicit_id_marks_it_archived(tmp_path):
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
    service.handle_webhook(None, "/new First idea", force_new=True)
    service.handle_webhook(None, "/new Second idea", force_new=True)

    result = service.cancel_project("first-idea")

    assert result["project_id"] == "first-idea"
    assert result["cancelled"] is True
    assert service.status("second-idea")["cancelled"] is False


def test_cancel_project_with_no_argument_cancels_the_active_project(service):
    service.handle_webhook("my-project", "hello")

    result = service.cancel_project(None)

    assert result["project_id"] == "my-project"
    assert result["cancelled"] is True


def test_cancel_project_clears_the_active_pointer_when_cancelling_the_active_project(service):
    service.handle_webhook("my-project", "hello")

    service.cancel_project("my-project")

    assert service._read_active_project_id() is None


def test_cancel_project_leaves_the_active_pointer_alone_when_cancelling_a_different_project(
    tmp_path,
):
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
    service.handle_webhook(None, "/new First idea", force_new=True)
    service.handle_webhook(None, "/new Second idea", force_new=True)

    service.cancel_project("first-idea")

    assert service._read_active_project_id() == "second-idea"


def test_cancel_project_raises_for_an_unknown_project(service):
    with pytest.raises(UnknownProjectError):
        service.cancel_project("does-not-exist")


def test_cancel_project_raises_when_no_argument_and_nothing_is_active(tmp_path):
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
        service.cancel_project(None)


def test_cancel_project_adopts_a_project_with_no_registry_entry(service):
    service.handle_webhook("my-project", "hello")

    result = service.cancel_project("my-project")

    assert result["cancelled"] is True
    records = load_registry(service._registry_path)
    assert records[0].id == "my-project"
    assert records[0].status == "archived"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest services/orchestrator/tests/unit/test_service.py -k "test_cancel_project" -v`
Expected: FAIL with `AttributeError: 'OrchestratorService' object has no attribute 'cancel_project'`

- [ ] **Step 3: Implement `cancel_project()`**

In `services/orchestrator/service.py`, add this new method right after `rename_active_project` (which currently ends with `return self.status(active_id)`):

```python
    def cancel_project(self, identifier: str | None) -> dict[str, Any]:
        """Cancels a project by marking its registry entry archived.

        Fully reversible: `resume()`, `activate()`, or `handle_webhook`'s
        explicit-id branch all revive it automatically the next time they
        actually run the graph for it (see `_unarchive_if_needed`). A
        project started before the registry was unified with Telegram's
        /new flow (or otherwise missing a registry entry) is adopted into
        the registry by this call, the same way `rename_active_project`
        already does -- its name defaults to its id (there is no better
        name to give it), mode to "greenfield", and path to the same
        `projects/<id>` location every registered project's path follows.

        Args:
            identifier: A project_id, a registered project's name
                (case-insensitive), or None to cancel the currently active
                project.

        Returns:
            The cancelled project's status dict (see `status()`), with
            `cancelled` now True.

        Raises:
            UnknownProjectError: If identifier resolves to no project with
                checkpoint history, or identifier is None and there is no
                active project.
        """
        if identifier is None:
            project_id = self._read_active_project_id()
            if project_id is None:
                raise UnknownProjectError("<no active project>")
        else:
            project_id = self._resolve_identifier(identifier)
        if not self._project_exists(project_id):
            raise UnknownProjectError(project_id)

        records = load_registry(self._registry_path)
        for index, record in enumerate(records):
            if record.id == project_id:
                records[index] = record.model_copy(update={"status": "archived"})
                break
        else:
            records.append(
                ProjectRecord(
                    id=project_id,
                    name=project_id,
                    mode="greenfield",
                    path=str(self._projects_root / project_id),
                    stack=_UNSPECIFIED_STACK,
                    status="archived",
                )
            )
        save_registry(self._registry_path, records)

        if self._read_active_project_id() == project_id:
            self._write_active_project_id("")

        return self.status(project_id)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest services/orchestrator/tests/unit/test_service.py -v`
Expected: PASS (full file)

- [ ] **Step 5: Lint and commit**

```bash
ruff check services/orchestrator/service.py services/orchestrator/tests/unit/test_service.py
ruff format services/orchestrator/service.py services/orchestrator/tests/unit/test_service.py
git add services/orchestrator/service.py services/orchestrator/tests/unit/test_service.py
git commit -m "feat(orchestrator): add cancel_project()"
```

---

## Task 4: `POST /cancel` + `cancelled` on the API response models

**Files:**
- Modify: `services/orchestrator/api.py`
- Test: `services/orchestrator/tests/unit/test_api.py`

**Interfaces:**
- Consumes: `service.cancel_project(identifier: str | None) -> dict[str, Any]` (Task 3), `UnknownProjectError`.
- Produces: `CancelRequest` Pydantic model, `POST /cancel` route. `ProjectStatusResponse.cancelled: bool`, `ProjectSummary.cancelled: bool`.

- [ ] **Step 1: Write the failing tests**

Add to `services/orchestrator/tests/unit/test_api.py`, near the other `/rename` tests (after `test_rename_with_a_taken_name_returns_409`):

```python
def test_cancel_marks_the_target_project_cancelled(tmp_path):
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
    client = TestClient(build_app(service))
    client.post("/webhook", json={"user_id": 1, "text": "hi", "project_id": "my-project"})

    response = client.post("/cancel", json={"project": "my-project"})

    assert response.status_code == 200
    assert response.json()["cancelled"] is True


def test_cancel_with_no_project_cancels_the_active_one(tmp_path):
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
    client = TestClient(build_app(service))
    client.post("/webhook", json={"user_id": 1, "text": "hi", "project_id": "my-project"})

    response = client.post("/cancel", json={})

    assert response.status_code == 200
    assert response.json()["project_id"] == "my-project"
    assert response.json()["cancelled"] is True


def test_cancel_unknown_project_returns_404(client):
    response = client.post("/cancel", json={"project": "does-not-exist"})

    assert response.status_code == 404


def test_status_includes_cancelled_field(client):
    client.post("/webhook", json={"user_id": 1, "text": "hi", "project_id": "my-project"})

    response = client.get("/status/my-project")

    assert response.json()["cancelled"] is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest services/orchestrator/tests/unit/test_api.py -k "cancel" -v`
Expected: FAIL -- `/cancel` 404s as an unknown route (no such endpoint yet), and `test_status_includes_cancelled_field` fails with `KeyError`

- [ ] **Step 3: Implement the API changes**

In `services/orchestrator/api.py`, add a new request model right after `RenameRequest` (which currently reads `name: str`):

```python
class CancelRequest(BaseModel):
    """Payload carrying an optional target project to cancel."""

    project: str | None = None
```

Update `ProjectStatusResponse`. It currently reads:

```python
class ProjectStatusResponse(BaseModel):
    """A project's current phase and any pending approval, rendered by the Gateway."""

    project_id: str
    name: str | None = None
    active: bool = False
    phase: str
    pending_approval: dict | None
    task_progress: dict | None
    text_applied: bool = True
```

Add one field, right after `active`:

```python
class ProjectStatusResponse(BaseModel):
    """A project's current phase and any pending approval, rendered by the Gateway."""

    project_id: str
    name: str | None = None
    active: bool = False
    cancelled: bool = False
    phase: str
    pending_approval: dict | None
    task_progress: dict | None
    text_applied: bool = True
```

Update `ProjectSummary` similarly. It currently reads:

```python
class ProjectSummary(BaseModel):
    """One project's id, registered name, and active status, as listed by GET /projects."""

    id: str
    name: str | None
    active: bool
```

Change to:

```python
class ProjectSummary(BaseModel):
    """One project's id, registered name, active, and cancelled status, as listed by GET /projects."""

    id: str
    name: str | None
    active: bool
    cancelled: bool
```

Finally, add the route. In `build_app()`, right after the existing `/rename` route (which currently ends with `return ProjectStatusResponse(**result)` inside the `rename` function, immediately before `return app`), add:

```python
    @app.post("/cancel", response_model=ProjectStatusResponse)
    def cancel(request: CancelRequest) -> ProjectStatusResponse:
        try:
            result = service.cancel_project(request.project)
        except UnknownProjectError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return ProjectStatusResponse(**result)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest services/orchestrator/tests/unit/test_api.py -v`
Expected: PASS (full file)

- [ ] **Step 5: Lint and commit**

```bash
ruff check services/orchestrator/api.py services/orchestrator/tests/unit/test_api.py
ruff format services/orchestrator/api.py services/orchestrator/tests/unit/test_api.py
git add services/orchestrator/api.py services/orchestrator/tests/unit/test_api.py
git commit -m "feat(orchestrator): add POST /cancel and cancelled response field"
```

---

## Task 5: `foundry cancel` CLI (`registry/manage_cli.py`)

**Files:**
- Modify: `services/orchestrator/registry/manage_cli.py`
- Modify: `foundry`
- Test: `services/orchestrator/registry/tests/unit/test_manage_cli.py`

**Interfaces:**
- Consumes: `ProjectRecord`, `load_registry`, `save_registry` (already imported in `manage_cli.py`), the existing `_resolve()` helper.
- Produces: `manage_cli.cancel_project(registry_path: Path, identifier: str) -> ProjectRecord`. `foundry cancel <id-or-name>`.

- [ ] **Step 1: Write the failing tests**

Add to `services/orchestrator/registry/tests/unit/test_manage_cli.py`, after `test_rename_project_allows_renaming_to_its_own_current_name`:

```python
def test_cancel_project_by_id_marks_it_archived(tmp_path):
    registry_path = tmp_path / "projects.yaml"
    _register(tmp_path, "my-project", "My Project")

    updated = manage_cli.cancel_project(registry_path, "my-project")

    assert updated.status == "archived"
    from services.orchestrator.registry.store import load_registry

    assert load_registry(registry_path)[0].status == "archived"


def test_cancel_project_by_name_is_case_insensitive(tmp_path):
    registry_path = tmp_path / "projects.yaml"
    _register(tmp_path, "my-project", "My Project")

    updated = manage_cli.cancel_project(registry_path, "my project")

    assert updated.id == "my-project"
    assert updated.status == "archived"


def test_cancel_project_raises_lookup_error_for_unknown_project(tmp_path):
    registry_path = tmp_path / "projects.yaml"
    _register(tmp_path, "my-project", "My Project")

    with pytest.raises(LookupError):
        manage_cli.cancel_project(registry_path, "does-not-exist")


def test_build_parser_parses_cancel():
    args = manage_cli.build_parser().parse_args(["cancel", "my-project"])

    assert args.command == "cancel"
    assert args.project == "my-project"


def test_main_cancel_updates_the_registry_and_prints_confirmation(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(manage_cli, "DEFAULT_PROJECTS_ROOT", tmp_path)
    monkeypatch.setattr(manage_cli, "DEFAULT_REGISTRY_PATH", tmp_path / "projects.yaml")
    _register(tmp_path, "my-project", "My Project")

    exit_code = manage_cli.main(["cancel", "my-project"])

    assert exit_code == 0
    assert "my-project" in capsys.readouterr().out


def test_main_cancel_returns_error_code_for_unknown_project(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(manage_cli, "DEFAULT_PROJECTS_ROOT", tmp_path)
    monkeypatch.setattr(manage_cli, "DEFAULT_REGISTRY_PATH", tmp_path / "projects.yaml")

    exit_code = manage_cli.main(["cancel", "does-not-exist"])

    assert exit_code == 1
    assert "unknown project" in capsys.readouterr().err
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest services/orchestrator/registry/tests/unit/test_manage_cli.py -k "cancel" -v`
Expected: FAIL with `AttributeError: module 'services.orchestrator.registry.manage_cli' has no attribute 'cancel_project'`

- [ ] **Step 3: Implement the CLI changes**

In `services/orchestrator/registry/manage_cli.py`, update `build_parser()`. It currently reads:

```python
def build_parser() -> argparse.ArgumentParser:
    """Builds the `projects`/`rename` argument parser.

    Returns:
        An argparse.ArgumentParser with a required `command` subparser
        ("projects" or "rename").
    """
    parser = argparse.ArgumentParser(prog="foundry")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("projects", help="List every registered project")
    rename_parser = subparsers.add_parser("rename", help="Rename a registered project")
    rename_parser.add_argument("project", help="The project's id or current registered name")
    rename_parser.add_argument("new_name", help="The new human-readable name")
    return parser
```

Replace with:

```python
def build_parser() -> argparse.ArgumentParser:
    """Builds the `projects`/`rename`/`cancel` argument parser.

    Returns:
        An argparse.ArgumentParser with a required `command` subparser
        ("projects", "rename", or "cancel").
    """
    parser = argparse.ArgumentParser(prog="foundry")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("projects", help="List every registered project")
    rename_parser = subparsers.add_parser("rename", help="Rename a registered project")
    rename_parser.add_argument("project", help="The project's id or current registered name")
    rename_parser.add_argument("new_name", help="The new human-readable name")
    cancel_parser = subparsers.add_parser("cancel", help="Cancel (archive) a registered project")
    cancel_parser.add_argument("project", help="The project's id or current registered name")
    return parser
```

Then add `cancel_project()` right after the existing `rename_project()` function (which currently ends with `return updated`):

```python
def cancel_project(registry_path: Path, identifier: str) -> ProjectRecord:
    """Cancels a registered project by setting its status to 'archived'.

    Args:
        registry_path: Path to the projects.yaml registry file.
        identifier: The project's id, or its current registered name
            (case-insensitive).

    Returns:
        The updated ProjectRecord.

    Raises:
        LookupError: If identifier matches no registered project.
    """
    records = load_registry(registry_path)
    target = _resolve(records, identifier)
    if target is None:
        raise LookupError(identifier)
    updated = target.model_copy(update={"status": "archived"})
    save_registry(
        registry_path,
        [updated if record.id == target.id else record for record in records],
    )
    return updated
```

Then add a private `_cancel()` runner right after the existing `_rename()` function (which currently ends with `return 0`):

```python
def _cancel(registry_path: Path, identifier: str) -> int:
    try:
        updated = cancel_project(registry_path, identifier)
    except LookupError:
        print(f"cancel: unknown project {identifier!r}", file=sys.stderr)
        return 1
    print(f"cancelled {updated.id!r} ({updated.name!r})")
    return 0
```

Finally, update `main()`. It currently reads:

```python
def main(argv: list[str] | None = None) -> int:
    """Parses arguments and runs the requested registry command.

    Args:
        argv: Command-line arguments, excluding the program name. Defaults
            to `sys.argv[1:]` when None.

    Returns:
        0 on success, 1 on a handled error (unknown project, duplicate name).
    """
    args = build_parser().parse_args(argv)
    if args.command == "projects":
        return _print_projects(DEFAULT_REGISTRY_PATH)
    return _rename(DEFAULT_REGISTRY_PATH, args.project, args.new_name)
```

Replace the body with:

```python
    args = build_parser().parse_args(argv)
    if args.command == "projects":
        return _print_projects(DEFAULT_REGISTRY_PATH)
    if args.command == "cancel":
        return _cancel(DEFAULT_REGISTRY_PATH, args.project)
    return _rename(DEFAULT_REGISTRY_PATH, args.project, args.new_name)
```

Now update the root `foundry` wrapper script. Add a usage line -- find the `usage()` function's here-doc and add, right after the existing `rename` line (`  rename        Rename a registered project: rename <id-or-name> <new-name>`):

```
  cancel        Cancel (archive) a registered project: cancel <id-or-name>
```

Then add a case branch right after the existing `rename)` case (which currently reads):

```bash
    rename)
        shift
        _require_orchestrator_running
        exec docker compose -f "$ROOT_DIR/docker-compose.yml" exec orchestrator \
            python3 -m services.orchestrator.registry.manage_cli rename "$@"
        ;;
```

Add immediately after it:

```bash
    cancel)
        shift
        _require_orchestrator_running
        exec docker compose -f "$ROOT_DIR/docker-compose.yml" exec orchestrator \
            python3 -m services.orchestrator.registry.manage_cli cancel "$@"
        ;;
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest services/orchestrator/registry/tests/unit/test_manage_cli.py -v`
Expected: PASS (full file)

Also run: `bash -n foundry` (from the repo root)
Expected: no output, exit code 0

- [ ] **Step 5: Lint and commit**

```bash
ruff check services/orchestrator/registry/manage_cli.py services/orchestrator/registry/tests/unit/test_manage_cli.py
ruff format services/orchestrator/registry/manage_cli.py services/orchestrator/registry/tests/unit/test_manage_cli.py
git add services/orchestrator/registry/manage_cli.py services/orchestrator/registry/tests/unit/test_manage_cli.py foundry
git commit -m "feat(cli): add foundry cancel"
```

---

## Task 6: `OrchestratorClient.cancel()`

**Files:**
- Modify: `services/telegram_gateway/orchestrator_client.py`
- Test: `services/telegram_gateway/tests/unit/test_orchestrator_client.py`

**Interfaces:**
- Consumes: nothing new (same `httpx.AsyncClient`/`self._base_url` pattern every other method in this file already uses).
- Produces: `OrchestratorClient.cancel(project: str | None = None) -> httpx.Response`.

- [ ] **Step 1: Write the failing test**

Add to `services/telegram_gateway/tests/unit/test_orchestrator_client.py`, after `test_rename_posts_the_new_name_to_the_rename_url`:

```python
def test_cancel_posts_the_target_project_to_the_cancel_url():
    client = _mock_httpx_client()
    orchestrator = OrchestratorClient("http://orchestrator:8000", client)

    asyncio.run(orchestrator.cancel("my-project"))

    args, kwargs = client.post.call_args
    assert args[0] == "http://orchestrator:8000/cancel"
    assert kwargs["json"] == {"project": "my-project"}


def test_cancel_defaults_to_no_target_project():
    client = _mock_httpx_client()
    orchestrator = OrchestratorClient("http://orchestrator:8000", client)

    asyncio.run(orchestrator.cancel())

    _, kwargs = client.post.call_args
    assert kwargs["json"] == {"project": None}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest services/telegram_gateway/tests/unit/test_orchestrator_client.py -k cancel -v`
Expected: FAIL with `AttributeError: 'OrchestratorClient' object has no attribute 'cancel'`

- [ ] **Step 3: Implement `cancel()`**

In `services/telegram_gateway/orchestrator_client.py`, add this method right after the existing `rename()` method (which currently ends with `return await self._client.post(f"{self._base_url}/rename", json={"name": name})`):

```python
    async def cancel(self, project: str | None = None) -> httpx.Response:
        """Cancels project (or the active project if None) via POST /cancel."""
        return await self._client.post(f"{self._base_url}/cancel", json={"project": project})
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest services/telegram_gateway/tests/unit/test_orchestrator_client.py -v`
Expected: PASS (full file)

- [ ] **Step 5: Lint and commit**

```bash
ruff check services/telegram_gateway/orchestrator_client.py services/telegram_gateway/tests/unit/test_orchestrator_client.py
ruff format services/telegram_gateway/orchestrator_client.py services/telegram_gateway/tests/unit/test_orchestrator_client.py
git add services/telegram_gateway/orchestrator_client.py services/telegram_gateway/tests/unit/test_orchestrator_client.py
git commit -m "feat(gateway): add OrchestratorClient.cancel()"
```

---

## Task 7: `(cancelled)` tag in rendering

**Files:**
- Modify: `services/telegram_gateway/rendering.py`
- Test: `services/telegram_gateway/tests/unit/test_rendering.py`

**Interfaces:**
- Consumes: nothing new -- same dict shapes `render_project_status`/`render_project_list`/`render_project_buttons` already take, now with an added `"cancelled": bool` key each dict may carry.
- Produces: no new public functions -- existing three render functions now also show a `(cancelled)` marker.

- [ ] **Step 1: Write the failing tests**

Add to `services/telegram_gateway/tests/unit/test_rendering.py`, after `test_render_project_status_marks_the_active_project`:

```python
def test_render_project_status_marks_a_cancelled_project():
    text = render_project_status(
        {"project_id": "my-project", "name": None, "phase": "implementing", "cancelled": True}
    )

    assert text == "my-project: implementing (cancelled)"


def test_render_project_status_marks_active_and_cancelled_together():
    text = render_project_status(
        {
            "project_id": "my-project",
            "name": None,
            "phase": "implementing",
            "active": True,
            "cancelled": True,
        }
    )

    assert text == "my-project: implementing (active) (cancelled)"
```

Add, after `test_render_project_list_marks_the_active_project`:

```python
def test_render_project_list_marks_a_cancelled_project():
    text = render_project_list(
        [{"id": "my-project", "name": None, "active": False, "cancelled": True}]
    )

    assert text == "Projects:\n- my-project (cancelled)"
```

Add, after `test_render_project_buttons_marks_the_active_project`:

```python
def test_render_project_buttons_marks_a_cancelled_project():
    keyboard = render_project_buttons(
        [{"id": "my-project", "name": None, "active": False, "cancelled": True}]
    )

    rows = keyboard.inline_keyboard
    assert [button.text for row in rows for button in row] == ["my-project (cancelled)"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest services/telegram_gateway/tests/unit/test_rendering.py -k cancelled -v`
Expected: FAIL -- all three assert the literal text without `(cancelled)` currently produced

- [ ] **Step 3: Implement the rendering changes**

In `services/telegram_gateway/rendering.py`, update `render_project_status`. It currently reads:

```python
    label = _project_label(body["project_id"], body.get("name"))
    active_suffix = " (active)" if body.get("active") else ""
    lines = [f"{label}: {body['phase']}{active_suffix}"]
    task_progress = body.get("task_progress")
```

Replace with:

```python
    label = _project_label(body["project_id"], body.get("name"))
    active_suffix = " (active)" if body.get("active") else ""
    cancelled_suffix = " (cancelled)" if body.get("cancelled") else ""
    lines = [f"{label}: {body['phase']}{active_suffix}{cancelled_suffix}"]
    task_progress = body.get("task_progress")
```

Update `render_project_list`. It currently reads:

```python
    if not projects:
        return "No projects yet. Send /new <description> to start one."
    return "Projects:\n" + "\n".join(
        f"{'-> ' if project.get('active') else '- '}"
        f"{_project_label(project['id'], project.get('name'))}"
        for project in projects
    )
```

Replace with:

```python
    if not projects:
        return "No projects yet. Send /new <description> to start one."
    return "Projects:\n" + "\n".join(
        f"{'-> ' if project.get('active') else '- '}"
        f"{_project_label(project['id'], project.get('name'))}"
        f"{' (cancelled)' if project.get('cancelled') else ''}"
        for project in projects
    )
```

Update `render_project_buttons`. It currently reads:

```python
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=(
                        f"{'-> ' if project.get('active') else ''}"
                        f"{_project_label(project['id'], project.get('name'))}"
                    ),
                    callback_data=f"{CALLBACK_ACTIVATE}:{project['id']}",
                )
            ]
            for project in projects
        ]
    )
```

Replace with:

```python
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=(
                        f"{'-> ' if project.get('active') else ''}"
                        f"{_project_label(project['id'], project.get('name'))}"
                        f"{' (cancelled)' if project.get('cancelled') else ''}"
                    ),
                    callback_data=f"{CALLBACK_ACTIVATE}:{project['id']}",
                )
            ]
            for project in projects
        ]
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest services/telegram_gateway/tests/unit/test_rendering.py -v`
Expected: PASS (full file)

- [ ] **Step 5: Lint and commit**

```bash
ruff check services/telegram_gateway/rendering.py services/telegram_gateway/tests/unit/test_rendering.py
ruff format services/telegram_gateway/rendering.py services/telegram_gateway/tests/unit/test_rendering.py
git add services/telegram_gateway/rendering.py services/telegram_gateway/tests/unit/test_rendering.py
git commit -m "feat(gateway): render a (cancelled) tag for cancelled projects"
```

---

## Task 8: `/cancel` Telegram command

**Files:**
- Modify: `services/telegram_gateway/bot.py`
- Test: `services/telegram_gateway/tests/unit/test_bot_handlers.py`

**Interfaces:**
- Consumes: `OrchestratorClient.cancel()` (Task 6), `_answer_timeout_hint`, `_answer_server_error` (already defined in `bot.py`).
- Produces: `handle_cancel(message: Message, orchestrator: OrchestratorClient) -> None`, registered on `Command("cancel")`.

- [ ] **Step 1: Write the failing tests**

First, add a mock for `cancel` to the shared `orchestrator` fixture in `services/telegram_gateway/tests/unit/test_bot_handlers.py`. It currently reads:

```python
    client.rename = AsyncMock(return_value=_status_response(project_id="proj-1", name="New Name"))
    return client
```

Change to:

```python
    client.rename = AsyncMock(return_value=_status_response(project_id="proj-1", name="New Name"))
    client.cancel = AsyncMock(
        return_value=_status_response(project_id="proj-1", name="Proj 1", cancelled=True)
    )
    return client
```

Then add the import for `handle_cancel` -- the existing import block reads:

```python
from services.telegram_gateway.bot import (
    handle_activate_callback,
    handle_approval_callback,
    handle_continue,
    handle_free_text,
    handle_help,
    handle_new,
    handle_projects,
    handle_rename,
    handle_reply_callback,
    handle_start,
    handle_status,
)
```

Add `handle_cancel` alphabetically:

```python
from services.telegram_gateway.bot import (
    handle_activate_callback,
    handle_approval_callback,
    handle_cancel,
    handle_continue,
    handle_free_text,
    handle_help,
    handle_new,
    handle_projects,
    handle_rename,
    handle_reply_callback,
    handle_start,
    handle_status,
)
```

Then add the tests themselves, after `test_handle_rename_shows_a_hint_when_the_orchestrator_is_unreachable` (search for it -- it's one of the `/rename` tests added in the earlier QoL batch):

```python
def test_handle_cancel_with_no_argument_cancels_the_active_project(message, orchestrator):
    message.text = "/cancel"

    asyncio.run(handle_cancel(message, orchestrator))

    orchestrator.cancel.assert_awaited_once_with(None)
    message.answer.assert_awaited_once_with('Cancelled "Proj 1". /continue to bring it back.')


def test_handle_cancel_with_an_argument_cancels_that_project(message, orchestrator):
    message.text = "/cancel my-project"

    asyncio.run(handle_cancel(message, orchestrator))

    orchestrator.cancel.assert_awaited_once_with("my-project")


def test_handle_cancel_falls_back_to_the_project_id_when_unnamed(message, orchestrator):
    message.text = "/cancel"
    orchestrator.cancel = AsyncMock(
        return_value=_status_response(project_id="proj-1", name=None, cancelled=True)
    )

    asyncio.run(handle_cancel(message, orchestrator))

    message.answer.assert_awaited_once_with('Cancelled "proj-1". /continue to bring it back.')


def test_handle_cancel_reports_unknown_project_on_404(message, orchestrator):
    message.text = "/cancel does-not-exist"
    orchestrator.cancel = AsyncMock(return_value=_status_response(status_code=404))

    asyncio.run(handle_cancel(message, orchestrator))

    message.answer.assert_awaited_once_with("Unknown project 'does-not-exist'. Try /projects.")


def test_handle_cancel_reports_no_active_project_on_404_with_no_argument(message, orchestrator):
    message.text = "/cancel"
    orchestrator.cancel = AsyncMock(return_value=_status_response(status_code=404))

    asyncio.run(handle_cancel(message, orchestrator))

    message.answer.assert_awaited_once_with("Unknown project 'the active project'. Try /projects.")


def test_handle_cancel_shows_a_hint_on_server_error(message, orchestrator):
    message.text = "/cancel my-project"
    response = MagicMock()
    response.status_code = 500
    response.json.side_effect = ValueError("not JSON")
    orchestrator.cancel = AsyncMock(return_value=response)

    asyncio.run(handle_cancel(message, orchestrator))

    message.answer.assert_awaited_once()
    response.json.assert_not_called()


def test_handle_cancel_shows_a_hint_when_the_orchestrator_is_unreachable(message, orchestrator):
    message.text = "/cancel my-project"
    orchestrator.cancel = AsyncMock(side_effect=httpx.ConnectError("connection refused"))

    asyncio.run(handle_cancel(message, orchestrator))

    message.answer.assert_awaited_once()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest services/telegram_gateway/tests/unit/test_bot_handlers.py -k cancel -v`
Expected: FAIL with `ImportError: cannot import name 'handle_cancel'`

- [ ] **Step 3: Implement `handle_cancel`**

In `services/telegram_gateway/bot.py`, add the handler right after `handle_rename` (which currently ends with `await message.answer(f'Renamed to "{response.json()["name"]}".')`), before the `@router.message(F.text)` / `handle_free_text` block:

```python
@router.message(Command("cancel"))
async def handle_cancel(message: Message, orchestrator: OrchestratorClient) -> None:
    """Cancels a project (archives it) via POST /cancel.

    With no argument, cancels the currently active project; with an
    argument, cancels that project by id or name -- no need to reactivate
    something first just to cancel it. Fully reversible: /continue on a
    cancelled project revives it (OrchestratorService._unarchive_if_needed).
    """
    parts = (message.text or "").split(maxsplit=1)
    target = parts[1].strip() if len(parts) > 1 else None
    try:
        response = await orchestrator.cancel(target)
    except (httpx.TimeoutException, httpx.ConnectError):
        await _answer_timeout_hint(message)
        return
    if response.status_code == 404:
        label = target or "the active project"
        await message.answer(f"Unknown project '{label}'. Try /projects.")
        return
    if response.status_code >= 500:
        await _answer_server_error(message)
        return
    body = response.json()
    label = body.get("name") or body["project_id"]
    await message.answer(f'Cancelled "{label}". /continue to bring it back.')
```

Then update `_HELP_TEXT`, which currently reads:

```python
_HELP_TEXT = (
    "Foundry commands:\n"
    "/new <description> -- start a new project\n"
    "/status [project] -- show a project's phase, or list all projects if omitted\n"
    "/continue <project> -- reactivate a project and show what it's waiting on\n"
    "/projects -- list every known project\n"
    "/rename <name> -- rename the active project\n"
    "/help -- show this message\n"
    "\n"
    "<project> accepts either a project's id or its registered name. Free "
    "text (no leading /) is treated as a reply to whatever the active "
    "project is currently waiting on."
)
```

Add a `/cancel` line right after the `/rename` line:

```python
_HELP_TEXT = (
    "Foundry commands:\n"
    "/new <description> -- start a new project\n"
    "/status [project] -- show a project's phase, or list all projects if omitted\n"
    "/continue <project> -- reactivate a project and show what it's waiting on\n"
    "/projects -- list every known project\n"
    "/rename <name> -- rename the active project\n"
    "/cancel [project] -- cancel a project (defaults to the active one); /continue undoes it\n"
    "/help -- show this message\n"
    "\n"
    "<project> accepts either a project's id or its registered name. Free "
    "text (no leading /) is treated as a reply to whatever the active "
    "project is currently waiting on."
)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest services/telegram_gateway/tests/unit/test_bot_handlers.py -v`
Expected: PASS (full file -- also re-confirms `test_handle_help_lists_commands_without_calling_orchestrator` still passes since it only checks for `/new`, `/status`, `/continue`, `/projects`, `/rename`, `/help` substrings, none of which changed)

- [ ] **Step 5: Lint and commit**

```bash
ruff check services/telegram_gateway/bot.py services/telegram_gateway/tests/unit/test_bot_handlers.py
ruff format services/telegram_gateway/bot.py services/telegram_gateway/tests/unit/test_bot_handlers.py
git add services/telegram_gateway/bot.py services/telegram_gateway/tests/unit/test_bot_handlers.py
git commit -m "feat(gateway): add /cancel command"
```

---

## Task 9: Decision Log entry + final verification

**Files:**
- Modify: `ARCHITECTURE.md`

**Interfaces:**
- Consumes: nothing (documentation only).
- Produces: nothing (documentation only).

- [ ] **Step 1: Find the last ADR number**

Run: `grep -n "^| 0[0-9][0-9] " ARCHITECTURE.md | tail -3`

(As of this plan being written, the last entry is ADR-055 -- confirm the actual current last number before writing the new one, in case other work landed in between.)

- [ ] **Step 2: Add the Decision Log entry**

In `ARCHITECTURE.md`, find the `## 10. Decision Log` table and add a new row right before the closing `---` that follows the table (the same place the two prior QoL-work ADRs were appended this session). Use the next sequential number after whatever Step 1 found -- assuming it's still 055, the new row is 056:

```markdown
| 056 | Project cancellation (QoL item #15) reuses `ProjectRecord.status` (`"active"`/`"archived"`, present since Phase 7, previously unused) rather than adding a new terminal `phase` written into the LangGraph checkpoint. `OrchestratorService.cancel_project()` sets it to `"archived"` (adopting an unregistered project into the registry first if needed, same pattern `rename_active_project` already uses); a new `_unarchive_if_needed()` check inside `_write_active_project_id()` flips it back to `"active"` the moment the project becomes active again via `resume()`, `activate()`, or `handle_webhook`'s explicit-id branch -- `status()` never revives, only acting on a project does. Deliberately not hooked into `_invoke_graph()` (the design's first draft): `activate()` only calls that for a genuinely *stuck* project (no `pending_approval`), so a project cancelled while paused at a normal gate -- the realistic case -- would never have been revived by `/continue`. `_write_active_project_id()` is called by every real revival path unconditionally, so hooking it there instead covers the actual common case. `/cancel [project]` (Telegram) and `foundry cancel <id-or-name>` (CLI) both expose cancellation; no confirmation step, since it's fully reversible via `/continue` and never touches checkpoint state | Full design: `docs/superpowers/specs/2026-08-30-cancel-abort-project-design.md`. Rejected a graph-state `"cancelled"` phase: it would touch every `Phase`-Literal match site and risks racing a genuinely in-flight `_graph.invoke()` call on the same thread if the target isn't actually paused, where the registry-only approach has no such risk and is sufficient for the realistic case (a project someone wants to cancel is almost always sitting at a `pending_approval` gate, not mid-task). Known accepted limitation: `foundry cancel` doesn't clear the Orchestrator's active-pointer file (private to `service.py`), so a CLI-cancelled project that also happens to be Telegram's active project could be silently revived by a stray free-text message before the operator notices -- narrow enough (both conditions must hold at once) to defer rather than factor shared infra for now |
```

- [ ] **Step 3: Commit the doc update**

```bash
git add ARCHITECTURE.md
git commit -m "docs: log the cancel/abort project decision (ADR-056)"
```

- [ ] **Step 4: Run the full suite and lint**

Run: `ruff check . && ruff format --check .`
Expected: `All checks passed!` and `<N> files already formatted`

Run: `python3 -m pytest --cov=services --cov-report=term-missing --cov-fail-under=80 -q`
Expected: coverage >= 80% (this repo has consistently run ~95%). Some `services/executor/tests/integration/` and `services/orchestrator/agents/tests/integration/test_coder_end_to_end.py` tests fail in any sandbox with no real Docker daemon available -- this is a pre-existing, environment-only failure class unrelated to this plan's changes (confirmed twice already this session on the same baseline). Every test this plan added or touched must be in the PASS list; if any of those specific tests fail, that's a real regression to fix before moving on, not something to wave through.

- [ ] **Step 5: Confirm nothing was left uncommitted**

Run: `git status --porcelain`
Expected: no output (clean tree) -- every task already committed its own changes.
