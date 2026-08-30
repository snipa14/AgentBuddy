"""FastAPI HTTP surface the Telegram Gateway calls (ARCHITECTURE.md §4.2)."""

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from services.orchestrator.progress_buffer import get_chunks_since
from services.orchestrator.registry.errors import DuplicateNameError, InvalidProjectIdError
from services.orchestrator.service import OrchestratorService, UnknownProjectError


class WebhookRequest(BaseModel):
    """Payload the Telegram Gateway forwards for every command or free-text message."""

    user_id: int
    text: str
    project_id: str | None = None
    force_new: bool = False


class ResumeRequest(BaseModel):
    """Payload carrying a human decision for a paused project's interrupt."""

    decision: str


class RenameRequest(BaseModel):
    """Payload carrying a new human-readable name for the active project."""

    name: str


class CancelRequest(BaseModel):
    """Payload carrying an optional target project to cancel."""

    project: str | None = None


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


class ProjectSummary(BaseModel):
    """One project's id, name, active status, and cancelled flag, as listed by GET /projects."""

    id: str
    name: str | None
    active: bool
    cancelled: bool = False


class ProjectListResponse(BaseModel):
    """The set of known projects."""

    projects: list[ProjectSummary]


class StreamChunk(BaseModel):
    """One buffered progress chunk, addressed by its sequence number."""

    seq: int
    text: str


class StreamResponse(BaseModel):
    """The set of progress chunks recorded for a project after `since_seq`."""

    chunks: list[StreamChunk]


class HealthResponse(BaseModel):
    """Liveness signal for docker-compose.yml's healthcheck and depends_on gates."""

    status: str = "ok"


def build_app(service: OrchestratorService) -> FastAPI:
    """Builds the Orchestrator's FastAPI app.

    Args:
        service: The OrchestratorService instance backing every route.

    Returns:
        A configured FastAPI application.
    """
    app = FastAPI(title="Foundry Orchestrator")

    @app.get("/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        """Reports liveness -- confirms the FastAPI server is up, nothing more."""
        return HealthResponse()

    @app.post("/webhook", response_model=ProjectStatusResponse)
    def webhook(request: WebhookRequest) -> ProjectStatusResponse:
        try:
            result = service.handle_webhook(
                request.project_id, request.text, force_new=request.force_new
            )
        except InvalidProjectIdError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return ProjectStatusResponse(**result)

    @app.post("/resume/{thread_id}", response_model=ProjectStatusResponse)
    def resume(thread_id: str, request: ResumeRequest) -> ProjectStatusResponse:
        try:
            result = service.resume(thread_id, request.decision)
        except UnknownProjectError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return ProjectStatusResponse(**result)

    @app.post("/activate/{project_id}", response_model=ProjectStatusResponse)
    def activate(project_id: str) -> ProjectStatusResponse:
        try:
            service.activate(project_id)
        except UnknownProjectError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return ProjectStatusResponse(**service.status(project_id))

    @app.get("/stream/{project_id}", response_model=StreamResponse)
    def stream(project_id: str, since_seq: int = -1) -> StreamResponse:
        chunks = get_chunks_since(project_id, since_seq)
        return StreamResponse(
            chunks=[StreamChunk(seq=chunk.seq, text=chunk.text) for chunk in chunks]
        )

    @app.get("/projects", response_model=ProjectListResponse)
    def list_projects() -> ProjectListResponse:
        return ProjectListResponse(
            projects=[ProjectSummary(**entry) for entry in service.list_projects()]
        )

    @app.get("/status/{project_id}", response_model=ProjectStatusResponse)
    def status(project_id: str) -> ProjectStatusResponse:
        try:
            result = service.status(project_id)
        except UnknownProjectError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return ProjectStatusResponse(**result)

    @app.post("/rename", response_model=ProjectStatusResponse)
    def rename(request: RenameRequest) -> ProjectStatusResponse:
        try:
            result = service.rename_active_project(request.name)
        except UnknownProjectError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except DuplicateNameError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return ProjectStatusResponse(**result)

    @app.post("/cancel", response_model=ProjectStatusResponse)
    def cancel(request: CancelRequest) -> ProjectStatusResponse:
        try:
            result = service.cancel_project(request.project)
        except UnknownProjectError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return ProjectStatusResponse(**result)

    return app
