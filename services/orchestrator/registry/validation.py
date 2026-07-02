"""project_id validation shared by every registration path."""

import re

from services.orchestrator.registry.errors import InvalidProjectIdError

PROJECT_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


def validate_project_id(project_id: str) -> None:
    """Validates that project_id is a safe, single path segment.

    Restricted to lowercase letters, digits, and hyphens (matching
    PROJECT_ID_PATTERN) rather than blocking `..`/`/` after the fact --
    an allow-list makes escaping the projects/ root structurally
    impossible instead of relying on catching every escape attempt.

    Args:
        project_id: The candidate project identifier.

    Raises:
        InvalidProjectIdError: If project_id doesn't match PROJECT_ID_PATTERN.
    """
    if not PROJECT_ID_PATTERN.match(project_id):
        raise InvalidProjectIdError(
            f"invalid project id {project_id!r}: must match {PROJECT_ID_PATTERN.pattern!r}"
        )
