"""Unit tests for resolve_project_path's traversal guard."""

import pytest

from services.executor.errors import PathTraversalError
from services.executor.paths import resolve_project_path


def test_resolve_project_path_returns_path_for_simple_relative_path(tmp_path):
    result = resolve_project_path(tmp_path, "src/app.py")

    assert result == (tmp_path / "src" / "app.py").resolve()


def test_resolve_project_path_returns_root_for_dot(tmp_path):
    assert resolve_project_path(tmp_path, ".") == tmp_path.resolve()


def test_resolve_project_path_raises_value_error_on_parent_traversal(tmp_path):
    with pytest.raises(ValueError):
        resolve_project_path(tmp_path, "../../etc/passwd")


def test_resolve_project_path_raises_path_traversal_error_on_parent_traversal(tmp_path):
    with pytest.raises(PathTraversalError):
        resolve_project_path(tmp_path, "../../etc/passwd")


def test_resolve_project_path_raises_on_absolute_path_escape(tmp_path):
    with pytest.raises(PathTraversalError):
        resolve_project_path(tmp_path, "/etc/passwd")


def test_resolve_project_path_allows_nested_traversal_that_stays_within_root(tmp_path):
    (tmp_path / "src").mkdir()

    result = resolve_project_path(tmp_path, "src/../src/app.py")

    assert result == (tmp_path / "src" / "app.py").resolve()
