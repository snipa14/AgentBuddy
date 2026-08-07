"""FastAPI HTTP surface the Orchestrator calls (ARCHITECTURE.md §4.3).

This is the "narrow internal API" §4.3 describes -- the Orchestrator (a
separate container/process, per docker-compose.yml) reaches every tool
function here over HTTP, never by importing services.executor directly,
which would mean constructing or holding a Docker client outside the one
service allowed to (.claude/rules/security.md).
"""

import logging

import docker
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from services.executor.config import ExecutorConfig
from services.executor.errors import CommandTimeoutError, ContainerExecutionError
from services.executor.schemas import CommandResult, FileEntry, ScreenshotResult, TestResult
from services.executor.tools import (
    tool_clear_project_cache,
    tool_list_files,
    tool_read_file,
    tool_run_command,
    tool_run_tests,
    tool_take_screenshot,
    tool_write_file,
)

logger = logging.getLogger(__name__)


class RunCommandRequest(BaseModel):
    """Payload for POST /run_command."""

    project_id: str
    command: list[str]
    timeout: int | None = None
    stack: str = "python"


class ReadFileRequest(BaseModel):
    """Payload for POST /read_file."""

    project_id: str
    path: str
    stack: str = "python"


class ReadFileResponse(BaseModel):
    """Response for POST /read_file."""

    content: str


class WriteFileRequest(BaseModel):
    """Payload for POST /write_file."""

    project_id: str
    path: str
    content: str
    stack: str = "python"


class WriteFileResponse(BaseModel):
    """Response for POST /write_file."""

    ok: bool


class ListFilesRequest(BaseModel):
    """Payload for POST /list_files."""

    project_id: str
    path: str = "."
    stack: str = "python"


class ListFilesResponse(BaseModel):
    """Response for POST /list_files."""

    entries: list[FileEntry]


class RunTestsRequest(BaseModel):
    """Payload for POST /run_tests."""

    project_id: str
    framework: str = "pytest"
    stack: str = "python"


class ClearProjectCacheRequest(BaseModel):
    """Payload for POST /clear_project_cache."""

    project_id: str
    stack: str


class ClearProjectCacheResponse(BaseModel):
    """Response for POST /clear_project_cache."""

    ok: bool


class HealthResponse(BaseModel):
    """Liveness signal for docker-compose.yml's healthcheck and depends_on gates."""

    status: str = "ok"


def build_app(client: docker.DockerClient, config: ExecutorConfig) -> FastAPI:
    """Builds the Executor's FastAPI app.

    Args:
        client: A Docker SDK client (see services.executor.docker_client).
        config: The Executor's runtime configuration.

    Returns:
        A configured FastAPI application.
    """
    app = FastAPI(title="Foundry Executor")

    @app.get("/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        """Reports liveness -- no Docker call, just confirms the FastAPI server is up."""
        return HealthResponse()

    @app.post("/run_command", response_model=CommandResult)
    def run_command(request: RunCommandRequest) -> CommandResult:
        """Runs a command inside a fresh workspace container for the request's project."""
        try:
            return tool_run_command(
                client,
                config,
                request.project_id,
                request.command,
                request.timeout,
                stack=request.stack,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except CommandTimeoutError as exc:
            logger.exception("project %s: run_command timed out", request.project_id)
            raise HTTPException(status_code=504, detail=str(exc)) from exc
        except ContainerExecutionError as exc:
            logger.exception("project %s: run_command failed", request.project_id)
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.post("/read_file", response_model=ReadFileResponse)
    def read_file(request: ReadFileRequest) -> ReadFileResponse:
        """Reads a UTF-8 text file from the request's project directory."""
        try:
            content = tool_read_file(
                client, config, request.project_id, request.path, stack=request.stack
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except CommandTimeoutError as exc:
            logger.exception("project %s: read_file timed out", request.project_id)
            raise HTTPException(status_code=504, detail=str(exc)) from exc
        except ContainerExecutionError as exc:
            logger.exception("project %s: read_file failed", request.project_id)
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return ReadFileResponse(content=content)

    @app.post("/write_file", response_model=WriteFileResponse)
    def write_file(request: WriteFileRequest) -> WriteFileResponse:
        """Writes a UTF-8 text file into the request's project directory."""
        try:
            ok = tool_write_file(
                client,
                config,
                request.project_id,
                request.path,
                request.content,
                stack=request.stack,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except CommandTimeoutError as exc:
            logger.exception("project %s: write_file timed out", request.project_id)
            raise HTTPException(status_code=504, detail=str(exc)) from exc
        except ContainerExecutionError as exc:
            logger.exception("project %s: write_file failed", request.project_id)
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return WriteFileResponse(ok=ok)

    @app.post("/list_files", response_model=ListFilesResponse)
    def list_files(request: ListFilesRequest) -> ListFilesResponse:
        """Lists the immediate entries of a directory in the request's project."""
        try:
            entries = tool_list_files(
                client, config, request.project_id, request.path, stack=request.stack
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except CommandTimeoutError as exc:
            logger.exception("project %s: list_files timed out", request.project_id)
            raise HTTPException(status_code=504, detail=str(exc)) from exc
        except ContainerExecutionError as exc:
            logger.exception("project %s: list_files failed", request.project_id)
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return ListFilesResponse(entries=entries)

    @app.post("/run_tests", response_model=TestResult)
    def run_tests(request: RunTestsRequest) -> TestResult:
        """Runs the request's project test suite inside a fresh workspace container."""
        try:
            return tool_run_tests(
                client, config, request.project_id, request.framework, stack=request.stack
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except CommandTimeoutError as exc:
            logger.exception("project %s: run_tests timed out", request.project_id)
            raise HTTPException(status_code=504, detail=str(exc)) from exc
        except ContainerExecutionError as exc:
            logger.exception("project %s: run_tests failed", request.project_id)
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    class TakeScreenshotRequest(BaseModel):
        """Payload for POST /take_screenshot."""

        project_id: str
        url: str = "http://localhost:5173"
        auto_start_server: bool = False
        breakpoint_width: int = 1280
        stack: str = "python"

    @app.post("/take_screenshot", response_model=ScreenshotResult)
    def take_screenshot(request: TakeScreenshotRequest) -> ScreenshotResult:
        """Captures a screenshot of a page served from the request's project."""
        try:
            return tool_take_screenshot(
                client,
                config,
                request.project_id,
                url=request.url,
                auto_start_server=request.auto_start_server,
                breakpoint_width=request.breakpoint_width,
                stack=request.stack,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except CommandTimeoutError as exc:
            logger.exception("project %s: take_screenshot timed out", request.project_id)
            raise HTTPException(status_code=504, detail=str(exc)) from exc
        except ContainerExecutionError as exc:
            logger.exception("project %s: take_screenshot failed", request.project_id)
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.post("/clear_project_cache", response_model=ClearProjectCacheResponse)
    def clear_project_cache(request: ClearProjectCacheRequest) -> ClearProjectCacheResponse:
        """Empties the request's project's package-manager cache volume."""
        try:
            ok = tool_clear_project_cache(client, config, request.project_id, request.stack)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except CommandTimeoutError as exc:
            logger.exception("project %s: clear_project_cache timed out", request.project_id)
            raise HTTPException(status_code=504, detail=str(exc)) from exc
        except ContainerExecutionError as exc:
            logger.exception("project %s: clear_project_cache failed", request.project_id)
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return ClearProjectCacheResponse(ok=ok)

    return app
