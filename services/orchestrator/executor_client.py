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
        self, project_id: str, command: list[str], timeout: int | None = None
    ) -> CommandResult:
        """Calls POST /run_command.

        Raises:
            ExecutorClientError: If the Executor returns a non-2xx response.
        """
        payload = {"project_id": project_id, "command": command, "timeout": timeout}
        data = await self._post("/run_command", payload)
        return CommandResult.model_validate(data)

    async def read_file(self, project_id: str, path: str) -> str:
        """Calls POST /read_file.

        Raises:
            ExecutorClientError: If the Executor returns a non-2xx response
                (including a 404 for a missing file).
        """
        data = await self._post("/read_file", {"project_id": project_id, "path": path})
        return data["content"]

    async def write_file(self, project_id: str, path: str, content: str) -> bool:
        """Calls POST /write_file.

        Raises:
            ExecutorClientError: If the Executor returns a non-2xx response.
        """
        payload = {"project_id": project_id, "path": path, "content": content}
        data = await self._post("/write_file", payload)
        return data["ok"]

    async def list_files(self, project_id: str, path: str = ".") -> list[FileEntry]:
        """Calls POST /list_files.

        Raises:
            ExecutorClientError: If the Executor returns a non-2xx response.
        """
        data = await self._post("/list_files", {"project_id": project_id, "path": path})
        return [FileEntry.model_validate(entry) for entry in data["entries"]]

    async def run_tests(self, project_id: str, framework: str = "pytest") -> TestResult:
        """Calls POST /run_tests.

        Raises:
            ExecutorClientError: If the Executor returns a non-2xx response.
        """
        payload = {"project_id": project_id, "framework": framework}
        data = await self._post("/run_tests", payload)
        return TestResult.model_validate(data)

    async def _post(self, path: str, payload: dict) -> dict:
        response = await self._client.post(f"{self._base_url}{path}", json=payload)
        if response.is_error:
            raise ExecutorClientError(
                f"POST {path} -> {response.status_code}: {response.text}"
            )
        return response.json()
