"""The Provider protocol every model backend implements."""

from typing import Protocol, runtime_checkable

from services.orchestrator.model_router.capabilities import Capability
from services.orchestrator.model_router.schemas import CompletionRequest, CompletionResult


@runtime_checkable
class Provider(Protocol):
    """A named backend capable of serving completion requests for one or more roles."""

    name: str

    def supports(self, capability: Capability) -> bool:
        """Reports whether this provider satisfies the given capability."""
        ...

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        """Sends a completion request to the underlying model and returns its result.

        Raises:
            ProviderRequestError: If the underlying API call fails.
        """
        ...
