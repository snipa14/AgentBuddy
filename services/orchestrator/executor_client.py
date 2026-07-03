"""Thin async HTTP client the Coder (and later Reviewer/Tester) agents use to
reach the Executor's typed tool API (ARCHITECTURE.md §4.3).

Defines its own response models rather than importing services.executor.schemas
-- matching services/telegram_gateway/orchestrator_client.py's relationship to
the Orchestrator: each service owns its side of the wire contract, so neither
service's deployment depends on the other's internal module layout, and the
Orchestrator never needs services.executor's Docker SDK dependency at all.
"""

import httpx
from pydantic import BaseModel


class ExecutorClientError(Exception):
    """Raised when the Executor's API returns an error response."""


class CommandResult(BaseModel):
    """The outcome of a run_command call inside a workspace container."""

    stdout: str
    stderr: str
    exit_code: int


class FileEntry(BaseModel):
    """A single entry returned by list_files."""

    name: str
    is_dir: bool


class TestResult(BaseModel):
    """The outcome of a run_tests call inside a workspace container."""

    passed: int
    failed: int
    coverage_pct: float | None
    report: str


class ScreenshotResult(BaseModel):
    """The outcome of a take_screenshot call inside a workspace container."""

    image_b64: str
    error: str | None = None


class ExecutorClient:
    """Wraps the Executor's five typed tool endpoints."""

    def __init__(self, base_url: str, client: httpx.AsyncClient) -> None:
        """Initializes the client.

        Args:
            base_url: The Executor's base URL (e.g. http://executor:8000).
            client: An httpx.AsyncClient, injected so tests can substitute a
                mocked transport instead of hitting the network.
        """
        self._base_url = base_url.rstrip("/")
        self._client = client

    async def run_command(
        self,
        project_id: str,
        command: list[str],
        timeout: int | None = None,
        stack: str = "python",
    ) -> CommandResult:
        """Calls POST /run_command.

        Args:
            project_id: The project whose workspace container to use.
            command: The command to run, as an argv list.
            timeout: Seconds to allow before killing the container.
            stack: The project's declared stack (ADR-028) -- selects the
                workspace image and cache volume.

        Raises:
            ExecutorClientError: If the Executor returns a non-2xx response.
        """
        payload = {
            "project_id": project_id,
            "command": command,
            "timeout": timeout,
            "stack": stack,
        }
        data = await self._post("/run_command", payload)
        return CommandResult.model_validate(data)

    async def read_file(self, project_id: str, path: str, stack: str = "python") -> str:
        """Calls POST /read_file.

        Raises:
            ExecutorClientError: If the Executor returns a non-2xx response
                (including a 404 for a missing file).
        """
        payload = {"project_id": project_id, "path": path, "stack": stack}
        data = await self._post("/read_file", payload)
        return data["content"]

    async def write_file(
        self, project_id: str, path: str, content: str, stack: str = "python"
    ) -> bool:
        """Calls POST /write_file.

        Raises:
            ExecutorClientError: If the Executor returns a non-2xx response.
        """
        payload = {"project_id": project_id, "path": path, "content": content, "stack": stack}
        data = await self._post("/write_file", payload)
        return data["ok"]

    async def list_files(
        self, project_id: str, path: str = ".", stack: str = "python"
    ) -> list[FileEntry]:
        """Calls POST /list_files.

        Raises:
            ExecutorClientError: If the Executor returns a non-2xx response.
        """
        payload = {"project_id": project_id, "path": path, "stack": stack}
        data = await self._post("/list_files", payload)
        return [FileEntry.model_validate(entry) for entry in data["entries"]]

    async def run_tests(
        self, project_id: str, framework: str = "pytest", stack: str = "python"
    ) -> TestResult:
        """Calls POST /run_tests.

        Args:
            project_id: The project whose workspace container to use.
            framework: "pytest" (default) or "npm".
            stack: The project's declared stack (ADR-028) -- selects the
                workspace image and cache volume.

        Raises:
            ExecutorClientError: If the Executor returns a non-2xx response.
        """
        payload = {"project_id": project_id, "framework": framework, "stack": stack}
        data = await self._post("/run_tests", payload)
        return TestResult.model_validate(data)

    async def take_screenshot(
        self,
        project_id: str,
        url: str = "http://localhost:5173",
        auto_start_server: bool = False,
        breakpoint_width: int = 1280,
        stack: str = "python",
    ) -> ScreenshotResult:
        """Calls POST /take_screenshot.

        Captures a screenshot of a web page running inside the workspace
        container and returns it as base64-encoded PNG data.

        Args:
            project_id: The project whose workspace container to use.
            url: The URL to capture (e.g. http://localhost:5173).
            auto_start_server: Whether to attempt starting a dev server if
                none is listening on *url*.
            breakpoint_width: Target viewport width for responsive sizing.
            stack: The project's declared stack (ADR-028) -- selects the
                workspace image and cache volume.

        Returns:
            A ScreenshotResult with base64-encoded PNG image data, or an
            error message if the capture failed.

        Raises:
            ExecutorClientError: If the Executor returns a non-2xx response.
        """
        payload = {
            "project_id": project_id,
            "url": url,
            "auto_start_server": auto_start_server,
            "breakpoint_width": breakpoint_width,
            "stack": stack,
        }
        data = await self._post("/take_screenshot", payload)
        return ScreenshotResult.model_validate(data)

    async def clear_project_cache(self, project_id: str, stack: str) -> bool:
        """Calls POST /clear_project_cache.

        Empties a project's package-manager cache volume's contents. Call
        this before removing project_id's own directory, as part of
        archiving/removing a project from the registry.

        Raises:
            ExecutorClientError: If the Executor returns a non-2xx response.
        """
        payload = {"project_id": project_id, "stack": stack}
        data = await self._post("/clear_project_cache", payload)
        return data["ok"]

    async def _post(self, path: str, payload: dict) -> dict:
        response = await self._client.post(f"{self._base_url}{path}", json=payload)
        if response.is_error:
            raise ExecutorClientError(
                f"POST {path} -> {response.status_code}: {response.text}"
            )
        return response.json()
