"""Typed exceptions raised by the model router."""


class ModelRouterError(Exception):
    """Base class for all model router errors."""


class UnknownRoleError(ModelRouterError):
    """Raised when resolve() is called with a role absent from the loaded config."""


class NoCapableProviderError(ModelRouterError):
    """Raised when no provider in a role's prefer list is both configured and capable."""


class ProviderRequestError(ModelRouterError):
    """Raised when a provider's underlying API call fails."""
