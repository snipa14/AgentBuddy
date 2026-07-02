"""Thin async HTTP client the Gateway uses to reach the Orchestrator's FastAPI surface."""

import httpx


class OrchestratorClient:
    """Wraps the two Orchestrator endpoints the Gateway calls (ARCHITECTURE.md §4.2).

    Holds no retry/caching logic of its own for v1 -- a thin, typed wrapper
    kept deliberately narrow so the Gateway stays free of business logic.
    """

    def __init__(self, base_url: str, client: httpx.AsyncClient) -> None:
        """Initializes the client.

        Args:
            base_url: The Orchestrator's base URL (e.g. http://orchestrator:8000).
            client: An httpx.AsyncClient, injected so tests can substitute a
                mocked client instead of hitting the network.
        """
        self._base_url = base_url.rstrip("/")
        self._client = client

    async def send_webhook(self, user_id: int, text: str) -> httpx.Response:
        """Forwards a free-text message or command to POST /webhook."""
        return await self._client.post(
            f"{self._base_url}/webhook", json={"user_id": user_id, "text": text}
        )

    async def resume(self, thread_id: str, decision: str) -> httpx.Response:
        """Resolves a paused graph thread via POST /resume/{thread_id}."""
        return await self._client.post(
            f"{self._base_url}/resume/{thread_id}", json={"decision": decision}
        )
