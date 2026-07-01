"""Capability vocabulary shared by role requirements and provider declarations."""

from enum import Enum


class Capability(str, Enum):
    """A trait a provider may support and a role may require.

    Providers declare which of these they satisfy explicitly in
    `config/models.yaml` (see ARCHITECTURE.md ADR-016); roles declare which
    ones they require. The router matches the two when resolving a role.
    """

    TOOL_CALLING = "tool_calling"
    LONG_CONTEXT = "long_context"
    VISION = "vision"
