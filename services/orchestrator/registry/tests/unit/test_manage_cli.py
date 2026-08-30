"""Unit tests for the `projects`/`rename` CLI entrypoints."""

import pytest

import services.orchestrator.registry.manage_cli as manage_cli
from services.orchestrator.registry.errors import DuplicateNameError
from services.orchestrator.registry.registration import register_greenfield


def _register(tmp_path, project_id: str, name: str) -> None:
    register_greenfield(
        tmp_path / "projects.yaml",
        tmp_path,
        project_id,
        name,
        "python",
    )


def test_build_parser_requires_a_subcommand():
    with pytest.raises(SystemExit):
        manage_cli.build_parser().parse_args([])


def test_build_parser_parses_projects():
    args = manage_cli.build_parser().parse_args(["projects"])

    assert args.command == "projects"


def test_build_parser_parses_rename():
    args = manage_cli.build_parser().parse_args(["rename", "my-project", "New Name"])

    assert args.command == "rename"
    assert args.project == "my-project"
    assert args.new_name == "New Name"


def test_rename_project_by_id_updates_the_registry(tmp_path):
    registry_path = tmp_path / "projects.yaml"
    _register(tmp_path, "my-project", "My Project")

    updated = manage_cli.rename_project(registry_path, "my-project", "New Name")

    assert updated.name == "New Name"
    from services.orchestrator.registry.store import load_registry

    assert load_registry(registry_path)[0].name == "New Name"


def test_rename_project_by_current_name_is_case_insensitive(tmp_path):
    registry_path = tmp_path / "projects.yaml"
    _register(tmp_path, "my-project", "My Project")

    updated = manage_cli.rename_project(registry_path, "my project", "New Name")

    assert updated.id == "my-project"
    assert updated.name == "New Name"


def test_rename_project_raises_lookup_error_for_unknown_project(tmp_path):
    registry_path = tmp_path / "projects.yaml"
    _register(tmp_path, "my-project", "My Project")

    with pytest.raises(LookupError):
        manage_cli.rename_project(registry_path, "does-not-exist", "New Name")


def test_rename_project_raises_duplicate_name_error_when_taken(tmp_path):
    registry_path = tmp_path / "projects.yaml"
    _register(tmp_path, "project-a", "First")
    _register(tmp_path, "project-b", "Second")

    with pytest.raises(DuplicateNameError):
        manage_cli.rename_project(registry_path, "project-b", "First")


def test_rename_project_allows_renaming_to_its_own_current_name(tmp_path):
    registry_path = tmp_path / "projects.yaml"
    _register(tmp_path, "my-project", "My Project")

    updated = manage_cli.rename_project(registry_path, "my-project", "My Project")

    assert updated.name == "My Project"


def test_cancel_project_by_id_marks_it_archived(tmp_path):
    registry_path = tmp_path / "projects.yaml"
    _register(tmp_path, "my-project", "My Project")

    updated = manage_cli.cancel_project(registry_path, "my-project")

    assert updated.status == "archived"
    from services.orchestrator.registry.store import load_registry

    assert load_registry(registry_path)[0].status == "archived"


def test_cancel_project_by_name_is_case_insensitive(tmp_path):
    registry_path = tmp_path / "projects.yaml"
    _register(tmp_path, "my-project", "My Project")

    updated = manage_cli.cancel_project(registry_path, "my project")

    assert updated.id == "my-project"
    assert updated.status == "archived"


def test_cancel_project_raises_lookup_error_for_unknown_project(tmp_path):
    registry_path = tmp_path / "projects.yaml"
    _register(tmp_path, "my-project", "My Project")

    with pytest.raises(LookupError):
        manage_cli.cancel_project(registry_path, "does-not-exist")


def test_build_parser_parses_cancel():
    args = manage_cli.build_parser().parse_args(["cancel", "my-project"])

    assert args.command == "cancel"
    assert args.project == "my-project"


def test_main_cancel_updates_the_registry_and_prints_confirmation(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(manage_cli, "DEFAULT_PROJECTS_ROOT", tmp_path)
    monkeypatch.setattr(manage_cli, "DEFAULT_REGISTRY_PATH", tmp_path / "projects.yaml")
    _register(tmp_path, "my-project", "My Project")

    exit_code = manage_cli.main(["cancel", "my-project"])

    assert exit_code == 0
    assert "my-project" in capsys.readouterr().out


def test_main_cancel_returns_error_code_for_unknown_project(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(manage_cli, "DEFAULT_PROJECTS_ROOT", tmp_path)
    monkeypatch.setattr(manage_cli, "DEFAULT_REGISTRY_PATH", tmp_path / "projects.yaml")

    exit_code = manage_cli.main(["cancel", "does-not-exist"])

    assert exit_code == 1
    assert "unknown project" in capsys.readouterr().err


def test_main_projects_prints_a_hint_when_registry_is_empty(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(manage_cli, "DEFAULT_REGISTRY_PATH", tmp_path / "projects.yaml")

    exit_code = manage_cli.main(["projects"])

    assert exit_code == 0
    assert "No projects registered yet" in capsys.readouterr().out


def test_main_projects_lists_registered_projects(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(manage_cli, "DEFAULT_PROJECTS_ROOT", tmp_path)
    monkeypatch.setattr(manage_cli, "DEFAULT_REGISTRY_PATH", tmp_path / "projects.yaml")
    _register(tmp_path, "my-project", "My Project")

    exit_code = manage_cli.main(["projects"])

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "my-project" in out
    assert "My Project" in out


def test_main_rename_updates_the_registry_and_prints_confirmation(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(manage_cli, "DEFAULT_PROJECTS_ROOT", tmp_path)
    monkeypatch.setattr(manage_cli, "DEFAULT_REGISTRY_PATH", tmp_path / "projects.yaml")
    _register(tmp_path, "my-project", "My Project")

    exit_code = manage_cli.main(["rename", "my-project", "New Name"])

    assert exit_code == 0
    assert "New Name" in capsys.readouterr().out


def test_main_rename_returns_error_code_for_unknown_project(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(manage_cli, "DEFAULT_PROJECTS_ROOT", tmp_path)
    monkeypatch.setattr(manage_cli, "DEFAULT_REGISTRY_PATH", tmp_path / "projects.yaml")

    exit_code = manage_cli.main(["rename", "does-not-exist", "New Name"])

    assert exit_code == 1
    assert "unknown project" in capsys.readouterr().err


def test_main_rename_returns_error_code_for_duplicate_name(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(manage_cli, "DEFAULT_PROJECTS_ROOT", tmp_path)
    monkeypatch.setattr(manage_cli, "DEFAULT_REGISTRY_PATH", tmp_path / "projects.yaml")
    _register(tmp_path, "project-a", "First")
    _register(tmp_path, "project-b", "Second")

    exit_code = manage_cli.main(["rename", "project-b", "First"])

    assert exit_code == 1
    assert "already used" in capsys.readouterr().err
