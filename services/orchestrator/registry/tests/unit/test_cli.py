"""Unit tests for the `new-project` CLI entrypoint.

`_start_intake` is monkeypatched to a no-op in every success-path test --
it lazily imports LangGraph, which isn't installed in every environment
this suite runs in, and it's exercised for real only via the intake graph
tests in `services/orchestrator/graph`.
"""

import subprocess

import pytest

import services.orchestrator.registry.cli as cli


def test_build_parser_parses_minimal_greenfield_args():
    args = cli.build_parser().parse_args(
        ["my-project", "--name", "My Project", "--stack", "python"]
    )

    assert args.project_id == "my-project"
    assert args.name == "My Project"
    assert args.stack == "python"
    assert args.coverage_threshold == 80
    assert args.pr_mode == "manual"
    assert args.from_path is None
    assert args.git_remote is None


def test_build_parser_rejects_both_from_path_and_git_remote():
    parser = cli.build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "my-project",
                "--name",
                "x",
                "--stack",
                "python",
                "--from-path",
                "/tmp/x",
                "--git-remote",
                "url",
            ]
        )


def test_main_registers_greenfield_project_and_starts_intake(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "DEFAULT_PROJECTS_ROOT", tmp_path)
    monkeypatch.setattr(cli, "DEFAULT_REGISTRY_PATH", tmp_path / "projects.yaml")
    started = []
    monkeypatch.setattr(
        cli, "_start_intake", lambda project_id, description: started.append(project_id)
    )

    exit_code = cli.main(["my-project", "--name", "My Project", "--stack", "python"])

    assert exit_code == 0
    assert started == ["my-project"]
    assert (tmp_path / "my-project").is_dir()


def test_main_registers_brownfield_project_from_local_path(tmp_path, monkeypatch):
    source = tmp_path / "existing-code"
    source.mkdir()
    monkeypatch.setattr(cli, "DEFAULT_PROJECTS_ROOT", tmp_path / "projects")
    monkeypatch.setattr(cli, "DEFAULT_REGISTRY_PATH", tmp_path / "projects.yaml")
    started = []
    monkeypatch.setattr(
        cli, "_start_intake", lambda project_id, description: started.append(project_id)
    )

    exit_code = cli.main(
        [
            "my-project",
            "--name",
            "My Project",
            "--stack",
            "python",
            "--from-path",
            str(source),
        ]
    )

    assert exit_code == 0
    assert started == ["my-project"]


def test_main_registers_brownfield_project_from_git_remote(tmp_path, monkeypatch):
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
    monkeypatch.setattr(cli, "DEFAULT_PROJECTS_ROOT", tmp_path / "projects")
    monkeypatch.setattr(cli, "DEFAULT_REGISTRY_PATH", tmp_path / "projects.yaml")
    started = []
    monkeypatch.setattr(
        cli, "_start_intake", lambda project_id, description: started.append(project_id)
    )

    exit_code = cli.main(
        [
            "my-project",
            "--name",
            "My Project",
            "--stack",
            "python",
            "--git-remote",
            str(source),
        ]
    )

    assert exit_code == 0
    assert started == ["my-project"]


def test_main_returns_error_code_and_message_on_duplicate(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "DEFAULT_PROJECTS_ROOT", tmp_path)
    monkeypatch.setattr(cli, "DEFAULT_REGISTRY_PATH", tmp_path / "projects.yaml")
    monkeypatch.setattr(cli, "_start_intake", lambda project_id, description: None)
    cli.main(["my-project", "--name", "My Project", "--stack", "python"])

    exit_code = cli.main(["my-project", "--name", "My Project", "--stack", "python"])

    assert exit_code == 1
    assert "already registered" in capsys.readouterr().err
