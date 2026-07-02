"""FastAPI HTTP surface the Telegram Gateway calls (ARCHITECTURE.md §4.2)."""

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from services.orchestrator.service import OrchestratorService, UnknownProjectError


class WebhookRequest(BaseModel):
    """Payload the Telegram Gateway forwards for every command or free-text message."""

    user_id: int
    text: str
    project_id: str | None = None


class ResumeRequest(BaseModel):
    """Payload carrying a human decision for a paused project's interrupt."""

    decision: str


class ProjectStatusResponse(BaseModel):
    """A project's current phase and any pending approval, rendered by the Gateway."""

    project_id: str
    phase: str
    pending_approval: dict | None


class ProjectListResponse(BaseModel):
    """The set of known project_ids."""

    project_ids: list[str]


def build_app(service: OrchestratorService) -> FastAPI:
    """Builds the Orchestrator's FastAPI app.

    Args:
        service: The OrchestratorService instance backing every route.

    Returns:
        A configured FastAPI application.
    """
    app = FastAPI(title="Foundry Orchestrator")

    @app.post("/webhook", response_model=ProjectStatusResponse)
    def webhook(request: WebhookRequest) -> ProjectStatusResponse:
        result = service.handle_webhook(request.project_id, request.text)
        return ProjectStatusResponse(**result)

    @app.post("/resume/{thread_id}", response_model=ProjectStatusResponse)
    def resume(thread_id: str, request: ResumeRequest) -> ProjectStatusResponse:
        try:
            result = service.resume(thread_id, request.decision)
        except UnknownProjectError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return ProjectStatusResponse(**result)

    @app.get("/projects", response_model=ProjectListResponse)
    def list_projects() -> ProjectListResponse:
        return ProjectListResponse(project_ids=service.list_projects())

    @app.get("/status/{project_id}", response_model=ProjectStatusResponse)
    def status(project_id: str) -> ProjectStatusResponse:
        try:
            result = service.status(project_id)
        except UnknownProjectError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return ProjectStatusResponse(**result)

    return app
