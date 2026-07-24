"""Unit tests for the workspace-image Dockerfile generator (ADR-045).

The golden tests assert that rendering each stack in config/stacks.yaml
reproduces the committed images/workspace-<stack>/Dockerfile byte-for-byte,
so hand-edits to a generated Dockerfile (hardening drift) fail CI. They
need pyyaml to load the real stacks.yaml, so they skip in environments
without it; the pure rendering tests below always run.
"""

from pathlib import Path

import pytest

from services.executor.imagegen import instruction_lines, render_dockerfile
from services.executor.stacks import StackConfig

REPO_ROOT = Path(__file__).resolve().parents[4]
TEMPLATE_PATH = REPO_ROOT / "images" / "workspace.template.Dockerfile"


def _python_stack() -> StackConfig:
    return StackConfig(
        image="foundry/workspace-python:3.12",
        base_image="python:3.12-slim",
        apt_packages=["build-essential", "git", "curl"],
        pip_packages=["pytest", "pytest-cov", "playwright", "ruff"],
    )


def _node_stack() -> StackConfig:
    return StackConfig(
        image="foundry/workspace-node:20",
        base_image="node:20-slim",
        apt_packages=["build-essential", "git", "curl", "python3", "python3-pip"],
        pip_packages=["playwright"],
        pip_break_system_packages=True,
        sandbox_user_mode="rename",
        rename_from="node",
    )


def test_render_python_contains_expected_instructions():
    rendered = render_dockerfile("python", _python_stack(), TEMPLATE_PATH.read_text())
    lines = instruction_lines(rendered)
    assert "FROM python:3.12-slim" in lines
    assert "RUN pip install --no-cache-dir pytest pytest-cov playwright ruff" in lines
    assert "USER 1000:1000" in lines
    assert any(line.startswith("RUN groupadd --gid 1000 sandbox") for line in lines)


def test_render_node_uses_rename_block_and_break_flag():
    rendered = render_dockerfile("node", _node_stack(), TEMPLATE_PATH.read_text())
    lines = instruction_lines(rendered)
    assert (
        "RUN pip install --break-system-packages --no-cache-dir playwright" in lines
    )
    assert any(line.startswith("RUN groupmod --new-name sandbox node") for line in lines)
    assert not any("useradd" in line for line in lines)


def test_render_env_lines_appear_for_go_style_stack():
    stack = StackConfig(
        image="foundry/workspace-go:1.22",
        base_image="golang:1.22-bookworm",
        apt_packages=["git", "curl", "python3", "python3-pip"],
        pip_packages=["playwright"],
        pip_break_system_packages=True,
        env={"GOMODCACHE": "/home/sandbox/go/pkg/mod"},
    )
    rendered = render_dockerfile("go", stack, TEMPLATE_PATH.read_text())
    assert "ENV GOMODCACHE=/home/sandbox/go/pkg/mod" in instruction_lines(rendered)


def test_render_has_no_unsubstituted_placeholders():
    rendered = render_dockerfile("python", _python_stack(), TEMPLATE_PATH.read_text())
    assert "$" not in rendered


def test_instruction_lines_drops_comments_and_blanks():
    text = "# comment\n\nFROM x\n  \nRUN a \\\n    && b\n"
    assert instruction_lines(text) == ["FROM x", "RUN a && b"]


def test_render_rejects_empty_apt_packages():
    stack = StackConfig(
        image="foundry/workspace-x:1",
        base_image="debian:12-slim",
        pip_packages=["playwright"],
    )
    with pytest.raises(ValueError, match="apt_packages"):
        render_dockerfile("x", stack, TEMPLATE_PATH.read_text())


def test_render_rejects_empty_pip_packages():
    stack = StackConfig(
        image="foundry/workspace-x:1",
        base_image="debian:12-slim",
        apt_packages=["git"],
    )
    with pytest.raises(ValueError, match="pip_packages"):
        render_dockerfile("x", stack, TEMPLATE_PATH.read_text())


def test_golden_rendered_matches_committed_dockerfiles():
    yaml = pytest.importorskip("yaml")
    del yaml
    from services.executor.stacks import load_stacks_config

    stacks = load_stacks_config(REPO_ROOT / "config" / "stacks.yaml").stacks
    template_text = TEMPLATE_PATH.read_text()
    for stack_name, stack in stacks.items():
        committed = (
            REPO_ROOT / "images" / f"workspace-{stack_name}" / "Dockerfile"
        ).read_text()
        assert render_dockerfile(stack_name, stack, template_text) == committed, (
            f"images/workspace-{stack_name}/Dockerfile is out of sync with the "
            "template -- rerun scripts/gen_workspace_images.py"
        )
