"""Errors raised during project registration."""


class RegistryError(Exception):
    """Base class for every project-registry error."""


class InvalidProjectIdError(RegistryError, ValueError):
    """Raised when a project_id isn't a safe, single path-segment slug."""


class DuplicateProjectIdError(RegistryError):
    """Raised when a project_id is already present in the registry."""


class DuplicateNameError(RegistryError):
    """Raised when a project name is already present in the registry (case-insensitive)."""


class BrownfieldSourceError(RegistryError):
    """Raised when a brownfield source (local path or git remote) can't be linked in."""
