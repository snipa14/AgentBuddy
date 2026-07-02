"""Unit tests for greenfield/brownfield project registration."""

import subprocess

import pytest

from services.orchestrator.memory.project_memory import read_memory_file
from services.orchestrator.registry.errors import (
    BrownfieldSourceError,
    DuplicateProjectIdError,
    InvalidProjectIdError,
)
from services.orchestrator.registry.registration import (
    register_brownfield_from_git,
    register_brownfield_from_local_path,
    register_greenfield,
)
from services.orchestrator.registry.store import load_registry


@pytest.fixture
def registry_path(tmp_path):
    return tmp_path / "projects.yaml"


@pytest.fixture
def projects_root(tmp_path):
    root = tmp_path / "projects"
    root.mkdir()
    return root


@pytest.fixture
def git_source_repo(tmp_path):
    source = tmp_path / "source-repo"
    source.mkdir()
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(
        ["git", "-C", str(source), "config", "user.email", "test@example.com"], check=True
    )
    subprocess.run(["git", "-C", str(source), "config", "user.name", "Test"], check=True)
    (source / "README.md").write_text("hello\n")
    subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    subprocess.run(["git", "-C", str(source), "commit", "-q", "-m", "initial"], check=True)
    return source


def test_register_greenfield_creates_project_dir_and_empty_memory_files(
    registry_path, projects_root
):
    record = register_greenfield(registry_path, projects_root, "my-project", "My Project", "python")

    assert record.mode == "greenfield"
    assert (projects_root / "my-project").is_dir()
    assert read_memory_file(projects_root, "my-project", "LEARNINGS.md") == ""
    assert load_registry(registry_path) == [record]


def test_register_greenfield_rejects_invalid_project_id(registry_path, projects_root):
    with pytest.raises(InvalidProjectIdError):
        register_greenfield(registry_path, projects_root, "../escape", "Bad", "python")


def test_register_greenfield_rejects_duplicate_project_id(registry_path, projects_root):
    register_greenfield(registry_path, projects_root, "my-project", "My Project", "python")

    with pytest.raises(DuplicateProjectIdError):
        register_greenfield(registry_path, projects_root, "my-project", "Again", "python")


def test_register_greenfield_invokes_on_registered_hook(registry_path, projects_root):
    started = []

    register_greenfield(
        registry_path, projects_root, "my-project", "My Project", "python",
        on_registered=started.append,
    )

    assert started == ["my-project"]


def test_register_greenfield_does_not_invoke_hook_on_duplicate(registry_path, projects_root):
    register_greenfield(registry_path, projects_root, "my-project", "My Project", "python")
    started = []

    with pytest.raises(DuplicateProjectIdError):
        register_greenfield(
            registry_path, projects_root, "my-project", "Again", "python",
            on_registered=started.append,
        )

    assert started == []


def test_register_brownfield_from_local_path_symlinks_source(
    registry_path, projects_root, tmp_path
):
    source = tmp_path / "existing-code"
    source.mkdir()
    (source / "main.py").write_text("print('hi')\n")

    record = register_brownfield_from_local_path(
        registry_path, projects_root, "my-project", "My Project", source, "python"
    )

    assert record.mode == "brownfield"
    project_dir = projects_root / "my-project"
    assert project_dir.is_symlink()
    assert (project_dir / "main.py").read_text() == "print('hi')\n"
    assert read_memory_file(projects_root, "my-project", "LEARNINGS.md") == ""


def test_register_brownfield_from_local_path_rejects_missing_source(
    registry_path, projects_root, tmp_path
):
    with pytest.raises(BrownfieldSourceError):
        register_brownfield_from_local_path(
            registry_path, projects_root, "my-project", "My Project",
            tmp_path / "does-not-exist", "python",
        )


def test_register_brownfield_from_git_clones_remote(
    registry_path, projects_root, git_source_repo
):
    record = register_brownfield_from_git(
        registry_path, projects_root, "my-project", "My Project", str(git_source_repo), "python"
    )

    assert record.mode == "brownfield"
    assert record.git_remote == str(git_source_repo)
    assert (projects_root / "my-project" / "README.md").read_text() == "hello\n"
    assert read_memory_file(projects_root, "my-project", "LEARNINGS.md") == ""


def test_register_brownfield_from_git_raises_on_clone_failure(
    registry_path, projects_root, tmp_path
):
    with pytest.raises(BrownfieldSourceError):
        register_brownfield_from_git(
            registry_path, projects_root, "my-project", "My Project",
            str(tmp_path / "not-a-repo"), "python",
        )
