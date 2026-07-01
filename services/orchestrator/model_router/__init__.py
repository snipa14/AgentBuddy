"""Capability-based agent-role -> LLM provider resolution.

See docs/model-router.md for usage and config/models.yaml for the default
provider/role configuration.
"""

from services.orchestrator.model_router.capabilities import Capability
from services.orchestrator.model_router.config import ModelsConfig, load_models_config
from services.orchestrator.model_router.errors import (
    ModelRouterError,
    NoCapableProviderError,
    ProviderRequestError,
    UnknownRoleError,
)
from services.orchestrator.model_router.provider import Provider
from services.orchestrator.model_router.router import ModelRouter
from services.orchestrator.model_router.schemas import (
    CompletionMessage,
    CompletionRequest,
    CompletionResult,
    TokenUsage,
)

__all__ = [
    "Capability",
    "CompletionMessage",
    "CompletionRequest",
    "CompletionResult",
    "ModelRouter",
    "ModelRouterError",
    "ModelsConfig",
    "NoCapableProviderError",
    "Provider",
    "ProviderRequestError",
    "TokenUsage",
    "UnknownRoleError",
    "load_models_config",
]
