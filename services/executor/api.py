"""FastAPI HTTP surface the Orchestrator calls (ARCHITECTURE.md §4.3).

This is the "narrow internal API" §4.3 describes -- the Orchestrator (a
separate container/process, per docker-compose.yml) reaches every tool
function here over HTTP, never by importing services.executor directly,
which would mean constructing or holding a Docker client outside the one
service allowed to (.claude/rules/security.md).
"""

import docker
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from services.executor.config import ExecutorConfig
from services.executor.errors import CommandTimeoutError, ContainerExecutionError
from services.executor.schemas import CommandResult, FileEntry, ScreenshotResult, TestResult
from services.executor.tools import (
    tool_list_files,
    tool_read_file,
    tool_run_command,
    tool_run_tests,
    tool_take_screenshot,
    tool_write_file,
)


class RunCommandRequest(BaseModel):
    """Payload for POST /run_command."""

    project_id: str
    command: list[str]
    timeout: int | None = None


class ReadFileRequest(BaseModel):
    """Payload for POST /read_file."""

    project_id: str
    path: str


class ReadFileResponse(BaseModel):
    """Response for POST /read_file."""

    content: str


class WriteFileRequest(BaseModel):
    """Payload for POST /write_file."""

    project_id: str
    path: str
    content: str


class WriteFileResponse(BaseModel):
    """Response for POST /write_file."""

    ok: bool


class ListFilesRequest(BaseModel):
    """Payload for POST /list_files."""

    project_id: str
    path: str = "."


class ListFilesResponse(BaseModel):
    """Response for POST /list_files."""

    entries: list[FileEntry]


class RunTestsRequest(BaseModel):
    """Payload for POST /run_tests."""

    project_id: str
    framework: str = "pytest"


def build_app(client: docker.DockerClient, config: ExecutorConfig) -> FastAPI:
    """Builds the Executor's FastAPI app.

    Args:
        client: A Docker SDK client (see services.executor.docker_client).
        config: The Executor's runtime configuration.

    Returns:
        A configured FastAPI application.
    """
    app = FastAPI(title="Foundry Executor")

    @app.post("/run_command", response_model=CommandResult)
    def run_command(request: RunCommandRequest) -> CommandResult:
        try:
            return tool_run_command(
                client, config, request.project_id, request.command, request.timeout
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except CommandTimeoutError as exc:
            raise HTTPException(status_code=504, detail=str(exc)) from exc
        except ContainerExecutionError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.post("/read_file", response_model=ReadFileResponse)
    def read_file(request: ReadFileRequest) -> ReadFileResponse:
        try:
            content = tool_read_file(client, config, request.project_id, request.path)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return ReadFileResponse(content=content)

    @app.post("/write_file", response_model=WriteFileResponse)
    def write_file(request: WriteFileRequest) -> WriteFileResponse:
        try:
            ok = tool_write_file(client, config, request.project_id, request.path, request.content)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return WriteFileResponse(ok=ok)

    @app.post("/list_files", response_model=ListFilesResponse)
    def list_files(request: ListFilesRequest) -> ListFilesResponse:
        try:
            entries = tool_list_files(client, config, request.project_id, request.path)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except ContainerExecutionError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return ListFilesResponse(entries=entries)

    @app.post("/run_tests", response_model=TestResult)
    def run_tests(request: RunTestsRequest) -> TestResult:
        try:
            return tool_run_tests(client, config, request.project_id, request.framework)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    class TakeScreenshotRequest(BaseModel):
        """Payload for POST /take_screenshot."""

        project_id: str
        url: str = "http://localhost:5173"
        auto_start_server: bool = False
        breakpoint_width: int = 1280

    @app.post("/take_screenshot", response_model=ScreenshotResult)
    def take_screenshot(request: TakeScreenshotRequest) -> ScreenshotResult:
        try:
            return tool_take_screenshot(
                client,
                config,
                request.project_id,
                url=request.url,
                auto_start_server=request.auto_start_server,
                breakpoint_width=request.breakpoint_width,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    return app
