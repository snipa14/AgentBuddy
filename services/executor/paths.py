"""Project-scoped path resolution with a hard traversal guard."""

from pathlib import Path

from services.executor.errors import PathTraversalError


def resolve_project_path(root: Path, requested_path: str) -> Path:
    """Resolves a caller-supplied path against an allowed root directory.

    Args:
        root: The absolute directory requested_path must stay within.
        requested_path: A path an agent tool call asked to read/write/list,
            expected to be relative to root. An absolute path or one
            containing ".." segments that escape root is rejected.

    Returns:
        The resolved absolute path, guaranteed to be root or a descendant of it.

    Raises:
        PathTraversalError: If the resolved path escapes root.
    """
    resolved_root = root.resolve()
    candidate = (root / requested_path).resolve()
    if candidate != resolved_root and resolved_root not in candidate.parents:
        raise PathTraversalError(f"path '{requested_path}' resolves outside allowed root '{root}'")
    return candidate
