"""Capability-based agent-role -> LLM provider resolution.

See docs/model-router.md for usage and config/models.yaml for the default
provider/role configuration.
"""

from services.orchestrator.model_router.capabilities import Capability
from services.orchestrator.model_router.config import ModelsConfig, load_models_config
from services.orchestrator.model_router.errors import (
    ModelRouterError,
    NoCapableProviderError,
    UnknownRoleError,
)
from services.orchestrator.model_router.router import ModelRouter

__all__ = [
    "Capability",
    "ModelRouter",
    "ModelRouterError",
    "ModelsConfig",
    "NoCapableProviderError",
    "UnknownRoleError",
    "load_models_config",
]
