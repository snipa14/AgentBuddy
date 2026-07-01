"""Typed exceptions raised by the Executor."""


class ExecutorError(Exception):
    """Base class for all Executor errors."""


class PathTraversalError(ExecutorError, ValueError):
    """Raised when a requested path resolves outside its allowed root.

    Subclasses ValueError so callers that only care about "bad input" can
    catch ValueError without importing this module.
    """


class ContainerExecutionError(ExecutorError):
    """Raised when a workspace container fails to start, exec, or be inspected."""


class CommandTimeoutError(ExecutorError):
    """Raised when a command exceeds its configured timeout inside a workspace container."""
