"""Unit tests for project_id slug validation."""

import pytest

from services.orchestrator.registry.errors import InvalidProjectIdError
from services.orchestrator.registry.validation import validate_project_id


@pytest.mark.parametrize("project_id", ["my-project", "proj1", "a"])
def test_validate_project_id_accepts_valid_slugs(project_id):
    validate_project_id(project_id)


@pytest.mark.parametrize(
    "project_id",
    [
        "",
        "../etc",
        "foo/bar",
        "/absolute",
        "Has-Uppercase",
        "has_underscore",
        "-starts-with-hyphen",
        "has space",
    ],
)
def test_validate_project_id_rejects_unsafe_or_malformed_ids(project_id):
    with pytest.raises(InvalidProjectIdError):
        validate_project_id(project_id)
