# Phase 22 — Stack Registry & Templated Workspace Images Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Adding a language from the operator's shortlist (Go first) becomes one `config/stacks.yaml` entry plus one operator-run image build — zero `.py` edits, zero security-boundary change (ADR-045).

**Architecture:** A single Pydantic-validated `config/stacks.yaml` becomes the source of truth for everything per-stack: workspace image name, Dockerfile build inputs, dependency manifests + install commands, package-cache directory, and static-check commands (consumed by Phase 23). Per-stack Dockerfiles are rendered by an operator-run generator from one shared hardening template, replacing the two hand-copied Dockerfiles. The Executor and Orchestrator each load the YAML with their own copy of the loader — the same deliberate inter-service duplication `executor_client.py` already uses (services talk over HTTP, never import each other).

**Tech Stack:** Python 3.12, Pydantic v2, PyYAML, `string.Template` (stdlib — no jinja2), pytest.

## Global Constraints

- Security boundary: the `docker-socket-proxy` environment block in `docker-compose.yml` must be **byte-identical** before and after this phase (`CONTAINERS=1 EXEC=1 IMAGES=1 POST=1`, all else `0`). No agent-reachable image build path may be added. Only `services/executor/` may import the Docker SDK (`guard_executor_imports.py` hook enforces this — which is also why every test in this plan that talks to a real daemon builds its client via `services.executor.docker_client.build_docker_client()` rather than importing the SDK).
- Code conventions (CLAUDE.md): no inline comments (only non-obvious workarounds with an issue reference, plus test-fixture defect documentation); Google-style docstrings on every public function/class; `snake_case`/`PascalCase`/`SCREAMING_SNAKE_CASE`; no bare `except`; Pydantic for all external data; type hints everywhere.
- Git: all commits go **directly on `release/v1`** (no feature branch, no worktree — established project workflow). Per-task commits use Conventional Commits (`feat(executor): ...`). The phase-end commit is `Phase 22: stack registry and templated workspace images`, tagged `phase-22-done` (lightweight).
- Sandbox reality: `pytest` and `pydantic` run here; `pyyaml`, `langgraph`, `langchain_core`, `ruff`, and a Docker daemon do **not** exist here and must not be installed (`block_host_installs.py` hook). Tests that need `yaml` use `pytest.importorskip("yaml")` so they run in CI and skip here. Docker-integration tests are written, marked `@pytest.mark.integration`, and verified by hand-trace here + a real run on the host later. Every task states which of its tests execute in-sandbox.
- Coverage floor is 80% (`--cov-fail-under=80` in CI); new modules ship with tests in the same commit.

---

### Task 1: Executor stacks schema + loader

**Files:**
- Create: `services/executor/stacks.py`
- Test: `services/executor/tests/unit/test_stacks.py`

**Interfaces:**
- Produces: `StackManifest(file: str, install_command: list[str])`, `StackCheck(command: list[str], requires_file: str | None)`, `StackConfig`, `StacksConfig(stacks: dict[str, StackConfig])`, `load_stacks_config(path: Path) -> StacksConfig`. Tasks 2–6 and the whole of Phase 23 consume these exact names.

- [ ] **Step 1: Write the failing tests**

`services/executor/tests/unit/test_stacks.py`:

```python
"""Unit tests for the stacks.yaml schema and loader (ADR-045)."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from services.executor.stacks import (
    StackCheck,
    StackConfig,
    StackManifest,
    StacksConfig,
    load_stacks_config,
)


def _minimal_stack() -> dict:
    return {
        "image": "foundry/workspace-python:3.12",
        "base_image": "python:3.12-slim",
    }


def test_stack_config_minimal_validates_with_defaults():
    stack = StackConfig.model_validate(_minimal_stack())
    assert stack.apt_packages == []
    assert stack.pip_packages == []
    assert stack.pip_break_system_packages is False
    assert stack.sandbox_user_mode == "create"
    assert stack.rename_from is None
    assert stack.env == {}
    assert stack.manifests == []
    assert stack.cache_dir is None
    assert stack.check_commands == []


def test_stack_config_full_entry_validates():
    stack = StackConfig.model_validate(
        {
            **_minimal_stack(),
            "apt_packages": ["git", "curl"],
            "pip_packages": ["playwright"],
            "pip_break_system_packages": True,
            "sandbox_user_mode": "rename",
            "rename_from": "node",
            "env": {"GOMODCACHE": "/home/sandbox/go/pkg/mod"},
            "manifests": [
                {"file": "package.json", "install_command": ["npm", "install"]}
            ],
            "cache_dir": "/home/sandbox/.npm",
            "check_commands": [
                {
                    "command": ["npx", "--no-install", "tsc", "--noEmit"],
                    "requires_file": "tsconfig.json",
                }
            ],
        }
    )
    assert stack.manifests[0] == StackManifest(
        file="package.json", install_command=["npm", "install"]
    )
    assert stack.check_commands[0] == StackCheck(
        command=["npx", "--no-install", "tsc", "--noEmit"],
        requires_file="tsconfig.json",
    )


def test_stack_config_rename_mode_requires_rename_from():
    with pytest.raises(ValidationError):
        StackConfig.model_validate(
            {**_minimal_stack(), "sandbox_user_mode": "rename"}
        )


def test_stack_config_relative_cache_dir_rejected():
    with pytest.raises(ValidationError):
        StackConfig.model_validate({**_minimal_stack(), "cache_dir": ".npm"})


def test_stack_config_empty_install_command_rejected():
    with pytest.raises(ValidationError):
        StackConfig.model_validate(
            {
                **_minimal_stack(),
                "manifests": [{"file": "go.mod", "install_command": []}],
            }
        )


def test_stack_config_empty_check_command_rejected():
    with pytest.raises(ValidationError):
        StackConfig.model_validate(
            {**_minimal_stack(), "check_commands": [{"command": []}]}
        )


def test_stack_config_test_command_defaults_to_none():
    stack = StackConfig.model_validate(_minimal_stack())
    assert stack.test_command is None


def test_stack_config_empty_test_command_rejected():
    with pytest.raises(ValidationError):
        StackConfig.model_validate({**_minimal_stack(), "test_command": []})


def test_stacks_config_empty_mapping_allowed():
    assert StacksConfig(stacks={}).stacks == {}


def test_load_stacks_config_round_trips(tmp_path: Path):
    pytest.importorskip("yaml")
    config_file = tmp_path / "stacks.yaml"
    config_file.write_text(
        "stacks:\n"
        "  python:\n"
        "    image: foundry/workspace-python:3.12\n"
        "    base_image: python:3.12-slim\n"
        "    cache_dir: /home/sandbox/.cache/pip\n"
    )
    loaded = load_stacks_config(config_file)
    assert set(loaded.stacks) == {"python"}
    assert loaded.stacks["python"].cache_dir == "/home/sandbox/.cache/pip"


def test_load_stacks_config_missing_file_raises(tmp_path: Path):
    pytest.importorskip("yaml")
    with pytest.raises(FileNotFoundError):
        load_stacks_config(tmp_path / "absent.yaml")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest services/executor/tests/unit/test_stacks.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'services.executor.stacks'`

- [ ] **Step 3: Write the implementation**

`services/executor/stacks.py`:

```python
"""Pydantic schema and loader for config/stacks.yaml (ADR-045).

One entry per supported project stack, declaring everything the platform
needs to know about that language: the workspace image the Executor runs it
in, the Dockerfile build inputs the image generator renders from
(services/executor/imagegen.py), the dependency manifests and install
commands the Executor applies before running project code, the
package-manager cache directory backing the per-project cache volume
(ADR-028), and the deterministic static-check commands Phase 23's
node_static_checks runs before LLM review. The Orchestrator loads the same
file through its own copy of this loader (services/orchestrator/stacks.py)
-- deliberate duplication over a shared import, mirroring executor_client's
existing pattern of keeping the two services decoupled.
"""

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator


class StackManifest(BaseModel):
    """A dependency manifest file and the install command it triggers.

    Attributes:
        file: The manifest's filename at the project root (e.g.
            "requirements.txt", "package.json", "go.mod").
        install_command: The argv list run inside the workspace container
            when the manifest is present.
    """

    file: str = Field(min_length=1)
    install_command: list[str] = Field(min_length=1)


class StackCheck(BaseModel):
    """A deterministic static-check command for Phase 23's checks node.

    Attributes:
        command: The argv list to run inside the workspace container.
        requires_file: A project-root file that must exist for this check to
            apply (e.g. "tsconfig.json" for tsc). None means the check
            always applies. A missing required file skips the check, never
            fails it.
    """

    command: list[str] = Field(min_length=1)
    requires_file: str | None = None


class StackConfig(BaseModel):
    """One stack's full declaration in stacks.yaml.

    Attributes:
        image: The workspace image tag the Executor launches for this stack.
        base_image: The upstream base image (pinned tag) the generator
            renders this stack's Dockerfile FROM.
        apt_packages: Extra apt packages baked into the image.
        pip_packages: pip packages baked into the image. Every workspace
            image needs at least Playwright here -- the Executor's own tools
            run Python scripts in every container regardless of the
            project's stack (ADR-028).
        pip_break_system_packages: Whether the image's pip install needs
            --break-system-packages (Debian-managed Python, e.g. the node
            and go base images).
        sandbox_user_mode: "create" runs groupadd/useradd for the sandbox
            user; "rename" renames an existing UID/GID-1000 user instead
            (node:20-slim ships a "node" user on that UID).
        rename_from: The existing username to rename when sandbox_user_mode
            is "rename".
        env: Extra ENV lines rendered into the Dockerfile (e.g. GOMODCACHE).
        manifests: Dependency manifests checked in order; the first one
            present in the project wins.
        cache_dir: The in-container package-manager cache directory the
            per-project named cache volume mounts over (ADR-028). None
            means the stack gets no cache volume.
        check_commands: Deterministic static checks for Phase 23.
        test_command: The argv list tool_run_tests runs for this stack.
            None falls back to the caller-supplied framework parameter's
            legacy pytest/npm behavior.
    """

    image: str = Field(min_length=1)
    base_image: str = Field(min_length=1)
    apt_packages: list[str] = Field(default_factory=list)
    pip_packages: list[str] = Field(default_factory=list)
    pip_break_system_packages: bool = False
    sandbox_user_mode: Literal["create", "rename"] = "create"
    rename_from: str | None = None
    env: dict[str, str] = Field(default_factory=dict)
    manifests: list[StackManifest] = Field(default_factory=list)
    cache_dir: str | None = None
    check_commands: list[StackCheck] = Field(default_factory=list)
    test_command: list[str] | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _validate_cross_field_rules(self) -> "StackConfig":
        if self.sandbox_user_mode == "rename" and not self.rename_from:
            raise ValueError("sandbox_user_mode 'rename' requires rename_from")
        if self.cache_dir is not None and not self.cache_dir.startswith("/"):
            raise ValueError(f"cache_dir must be absolute, got {self.cache_dir!r}")
        return self


class StacksConfig(BaseModel):
    """The full, validated contents of a stacks.yaml file."""

    stacks: dict[str, StackConfig]


def load_stacks_config(path: Path) -> StacksConfig:
    """Loads and validates a stacks.yaml file.

    Imports yaml lazily, inside the function body rather than at module load
    time, so this module's Pydantic schema stays importable and testable
    even when pyyaml isn't installed -- the same fix
    services/orchestrator/registry/cli.py applies for langgraph.

    Args:
        path: Path to a YAML file matching the StacksConfig schema.

    Returns:
        A validated StacksConfig instance.

    Raises:
        FileNotFoundError: If path does not exist.
        pydantic.ValidationError: If the file's contents don't match the schema.
    """
    import yaml

    raw = yaml.safe_load(path.read_text())
    return StacksConfig.model_validate(raw)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest services/executor/tests/unit/test_stacks.py -v`
Expected: all PASS except `test_load_stacks_config_round_trips` / `test_load_stacks_config_missing_file_raises` which SKIP in this sandbox (no pyyaml). Both run in CI.

- [ ] **Step 5: Commit**

```bash
git add services/executor/stacks.py services/executor/tests/unit/test_stacks.py
git commit -m "feat(executor): stacks.yaml Pydantic schema and loader (ADR-045)"
```

---

### Task 2: `config/stacks.yaml` with python, node, and go

**Files:**
- Create: `config/stacks.yaml`

**Interfaces:**
- Consumes: the Task 1 schema — the file must validate against `StacksConfig`.
- Produces: the canonical three-stack registry every later task reads. The python/node entries must reproduce today's hardcoded behavior in `services/executor/config.py` (`STACK_IMAGES`) and `services/executor/containers.py` (`CACHE_VOLUME_TARGET_FOR_STACK`, `MANIFEST_INSTALL_COMMANDS_FOR_STACK`) exactly, with three deliberate additions: `ruff` in the python image (needed by its Phase 23 check), per-stack `check_commands`, and per-stack `test_command` (which makes `tool_run_tests` registry-driven in Task 5).

- [ ] **Step 1: Write the file**

`config/stacks.yaml`:

```yaml
# The stack registry (ADR-045): one entry per supported project language.
# Adding a language is one entry here plus one operator-run image build
# (scripts/gen_workspace_images.py + docker build) -- never a .py edit and
# never a docker-socket-proxy change. Validated against
# services/executor/stacks.py's StacksConfig at Executor and Orchestrator
# startup, same pattern as models.yaml.

stacks:
  python:
    image: foundry/workspace-python:3.12
    base_image: python:3.12-slim
    apt_packages: [build-essential, git, curl]
    pip_packages: [pytest, pytest-cov, playwright, ruff]
    pip_break_system_packages: false
    sandbox_user_mode: create
    manifests:
      - file: requirements.txt
        install_command: [pip, install, --user, -r, requirements.txt]
      - file: pyproject.toml
        install_command: [pip, install, --user, -e, "."]
    cache_dir: /home/sandbox/.cache/pip
    check_commands:
      - command: [ruff, check, .]
    test_command: [python3, -m, pytest, --cov=., --cov-report=term-missing, -q]

  node:
    image: foundry/workspace-node:20
    base_image: node:20-slim
    apt_packages: [build-essential, git, curl, python3, python3-pip]
    pip_packages: [playwright]
    pip_break_system_packages: true
    sandbox_user_mode: rename
    rename_from: node
    manifests:
      - file: package.json
        install_command: [npm, install]
    cache_dir: /home/sandbox/.npm
    check_commands:
      - command: [npx, --no-install, tsc, --noEmit]
        requires_file: tsconfig.json
    test_command: [npm, test]

  go:
    image: foundry/workspace-go:1.22
    base_image: golang:1.22-bookworm
    apt_packages: [git, curl, python3, python3-pip]
    pip_packages: [playwright]
    pip_break_system_packages: true
    sandbox_user_mode: create
    env:
      GOMODCACHE: /home/sandbox/go/pkg/mod
      GOCACHE: /home/sandbox/.cache/go-build
    manifests:
      - file: go.mod
        install_command: [go, mod, download]
    cache_dir: /home/sandbox/go/pkg/mod
    check_commands:
      - command: [go, vet, ./...]
      - command: [go, build, ./...]
    test_command: [go, test, ./...]
```

- [ ] **Step 2: Verify it validates**

In CI/on host: `python3 -c "from pathlib import Path; from services.executor.stacks import load_stacks_config; c = load_stacks_config(Path('config/stacks.yaml')); print(sorted(c.stacks))"`
Expected: `['go', 'node', 'python']`

In this sandbox (no pyyaml): hand-validate field-by-field against the Task 1 schema instead, and state in the commit message that the load was hand-traced.

- [ ] **Step 3: Commit**

```bash
git add config/stacks.yaml
git commit -m "feat(config): stacks.yaml registry for python, node, go (ADR-045)"
```

---

### Task 3: Dockerfile template + generator with golden tests

**Files:**
- Create: `images/workspace.template.Dockerfile`
- Create: `services/executor/imagegen.py`
- Create: `scripts/gen_workspace_images.py`
- Test: `services/executor/tests/unit/test_imagegen.py`

**Interfaces:**
- Consumes: `StackConfig` from Task 1.
- Produces: `render_dockerfile(stack_name: str, stack: StackConfig, template_text: str) -> str` and `instruction_lines(dockerfile_text: str) -> list[str]` in `services/executor/imagegen.py`. Task 4 regenerates the shipped Dockerfiles with the script.

- [ ] **Step 1: Write the template**

`images/workspace.template.Dockerfile` (rendered with `string.Template`; `$`-placeholders):

```dockerfile
# GENERATED FILE -- do not edit by hand. Rendered from
# images/workspace.template.Dockerfile by scripts/gen_workspace_images.py
# using config/stacks.yaml's "$stack_name" entry (ADR-045).
#
# Base image for ephemeral workspace containers spawned by the Executor
# (ARCHITECTURE.md 4.3). Every container built from this image runs
# non-root, with all capabilities dropped and no-new-privileges set by the
# Executor at container-creation time (services/executor/containers.py);
# this template carries the parts of least-privilege that belong at build
# time -- a non-root sandbox user at UID/GID 1000 and no leftover
# setuid/setgid binaries -- plus the Python3 + Playwright/Chromium layer
# the Executor's tools require in every image regardless of the project's
# own stack (ADR-028).

FROM $base_image

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
$apt_package_lines
    && rm -rf /var/lib/apt/lists/*

RUN pip install$pip_flags --no-cache-dir $pip_packages

ENV PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright
RUN playwright install --with-deps chromium \
    && chmod -R a+rX /opt/ms-playwright
$env_lines
RUN find / -xdev -perm /6000 -type f -exec chmod a-s {} + 2>/dev/null || true

$sandbox_user_block
WORKDIR /workspace
USER 1000:1000
```

- [ ] **Step 2: Write the failing tests**

`services/executor/tests/unit/test_imagegen.py`:

```python
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
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `python3 -m pytest services/executor/tests/unit/test_imagegen.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'services.executor.imagegen'`

- [ ] **Step 4: Write the implementation**

`services/executor/imagegen.py`:

```python
"""Renders per-stack workspace Dockerfiles from the shared template (ADR-045).

Operator-side tooling, invoked via scripts/gen_workspace_images.py -- the
Executor never calls this at runtime, and nothing agent-reachable can
trigger an image build (ADR-028: docker-socket-proxy keeps BUILD=0). It
lives in the executor package because the Executor owns workspace images,
and because that makes the rendering logic unit-testable under the normal
services/ coverage run.
"""

import string
from pathlib import Path

from services.executor.stacks import StackConfig, StacksConfig

_APT_INDENT = "        "
_CREATE_SANDBOX_BLOCK = (
    "RUN groupadd --gid 1000 sandbox \\\n"
    "    && useradd --uid 1000 --gid sandbox --create-home "
    "--shell /usr/sbin/nologin sandbox\n"
)
_RENAME_SANDBOX_BLOCK = (
    "RUN groupmod --new-name sandbox {rename_from} \\\n"
    "    && usermod --login sandbox --home /home/sandbox --move-home \\\n"
    "       --shell /usr/sbin/nologin {rename_from}\n"
)


def render_dockerfile(stack_name: str, stack: StackConfig, template_text: str) -> str:
    """Renders one stack's Dockerfile from the shared template.

    Args:
        stack_name: The stack's key in stacks.yaml (used in the generated
            header comment).
        stack: The stack's validated configuration.
        template_text: The contents of images/workspace.template.Dockerfile.

    Returns:
        The complete Dockerfile text, with runs of blank lines collapsed so
        stacks without extra env lines don't render double blanks.
    """
    apt_package_lines = "\n".join(
        f"{_APT_INDENT}{package} \\" for package in stack.apt_packages
    )
    env_lines = "".join(
        f"\nENV {key}={value}" for key, value in sorted(stack.env.items())
    )
    if env_lines:
        env_lines += "\n"
    if stack.sandbox_user_mode == "rename":
        sandbox_user_block = _RENAME_SANDBOX_BLOCK.format(
            rename_from=stack.rename_from
        )
    else:
        sandbox_user_block = _CREATE_SANDBOX_BLOCK
    rendered = string.Template(template_text).substitute(
        stack_name=stack_name,
        base_image=stack.base_image,
        apt_package_lines=apt_package_lines,
        pip_flags=" --break-system-packages" if stack.pip_break_system_packages else "",
        pip_packages=" ".join(stack.pip_packages),
        env_lines=env_lines,
        sandbox_user_block=sandbox_user_block,
    )
    return _collapse_blank_runs(rendered)


def instruction_lines(dockerfile_text: str) -> list[str]:
    """Reduces a Dockerfile to its logical instruction lines.

    Drops comments and blank lines and joins backslash continuations, so
    two Dockerfiles can be compared on what they actually instruct the
    daemon to do rather than on prose.

    Args:
        dockerfile_text: Full Dockerfile contents.

    Returns:
        One string per logical instruction.
    """
    logical: list[str] = []
    pending = ""
    for raw_line in dockerfile_text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.endswith("\\"):
            pending += line[:-1].rstrip() + " "
            continue
        logical.append((pending + line).strip())
        pending = ""
    if pending:
        logical.append(pending.strip())
    return logical


def generate_all(
    stacks: StacksConfig, template_path: Path, images_root: Path
) -> dict[str, Path]:
    """Renders and writes every stack's Dockerfile under images_root.

    Args:
        stacks: The loaded stack registry.
        template_path: Path to images/workspace.template.Dockerfile.
        images_root: The images/ directory to write
            workspace-<stack>/Dockerfile files into.

    Returns:
        A mapping of stack name to the Dockerfile path written.
    """
    template_text = template_path.read_text()
    written: dict[str, Path] = {}
    for stack_name, stack in stacks.stacks.items():
        target = images_root / f"workspace-{stack_name}" / "Dockerfile"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(render_dockerfile(stack_name, stack, template_text))
        written[stack_name] = target
    return written


def _collapse_blank_runs(text: str) -> str:
    """Collapses runs of two or more blank lines into a single blank line."""
    lines = text.splitlines()
    collapsed: list[str] = []
    for line in lines:
        if line.strip() == "" and collapsed and collapsed[-1].strip() == "":
            continue
        collapsed.append(line)
    return "\n".join(collapsed) + "\n"
```

`scripts/gen_workspace_images.py`:

```python
"""Operator CLI: regenerate images/workspace-<stack>/Dockerfile from config/stacks.yaml.

Run from the repo root after editing config/stacks.yaml, then build the
image(s) with docker build (CLAUDE.md's Commands section). This is the only
path that produces workspace Dockerfiles -- they are generated files, and
services/executor/tests/unit/test_imagegen.py's golden test fails CI if a
committed Dockerfile drifts from its rendered form.
"""

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from services.executor.imagegen import generate_all
from services.executor.stacks import load_stacks_config


def main(argv: list[str] | None = None) -> int:
    """Parses arguments and regenerates every stack's Dockerfile.

    Args:
        argv: Command-line arguments, excluding the program name. Defaults
            to sys.argv[1:] when None.

    Returns:
        0 on success.
    """
    parser = argparse.ArgumentParser(prog="gen_workspace_images")
    parser.add_argument(
        "--stacks-config", type=Path, default=REPO_ROOT / "config" / "stacks.yaml"
    )
    parser.add_argument(
        "--template",
        type=Path,
        default=REPO_ROOT / "images" / "workspace.template.Dockerfile",
    )
    parser.add_argument("--images-root", type=Path, default=REPO_ROOT / "images")
    args = parser.parse_args(argv)

    stacks = load_stacks_config(args.stacks_config)
    written = generate_all(stacks, args.template, args.images_root)
    for stack_name, path in sorted(written.items()):
        print(f"{stack_name}: {path.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python3 -m pytest services/executor/tests/unit/test_imagegen.py -v`
Expected: the five pure rendering tests PASS; `test_golden_rendered_matches_committed_dockerfiles` SKIPs here (no pyyaml) — it will pass in CI only after Task 4 commits the regenerated Dockerfiles. If running with pyyaml available before Task 4, it correctly FAILS: that ordering is intentional.

- [ ] **Step 6: Commit**

```bash
git add images/workspace.template.Dockerfile services/executor/imagegen.py scripts/gen_workspace_images.py services/executor/tests/unit/test_imagegen.py
git commit -m "feat(executor): workspace Dockerfile template and generator (ADR-045)"
```

---

### Task 4: Regenerate shipped Dockerfiles and prove security-posture equivalence

**Files:**
- Modify: `images/workspace-python/Dockerfile` (regenerated)
- Modify: `images/workspace-node/Dockerfile` (regenerated)
- Create: `images/workspace-go/Dockerfile` (generated)

**Interfaces:**
- Consumes: Task 3's generator and Task 2's `config/stacks.yaml`.
- Produces: the three committed Dockerfiles the golden test locks to the template.

This task's equivalence proof is the ADR-045 requirement: the template must reproduce today's security posture before any new stack ships. `pyyaml` is required, so on a host/CI environment run the generator; in this sandbox, produce the rendered output by calling `render_dockerfile` per stack with `StackConfig` objects hand-built from `config/stacks.yaml`'s values (no yaml import needed) via a short throwaway script in the scratchpad, and write the files with that.

- [ ] **Step 1: Render the new Dockerfiles**

Run (host/CI): `python3 scripts/gen_workspace_images.py`
Expected output:
```
go: images/workspace-go/Dockerfile
node: images/workspace-node/Dockerfile
python: images/workspace-python/Dockerfile
```

- [ ] **Step 2: Prove instruction-level equivalence with the pre-template files**

Compare each regenerated file's logical instructions with the shipped version at `git show HEAD` using `instruction_lines()` (or `diff <(grep -vE '^\s*(#|$)' ...)` on joined-continuation output). Expected result, which must be recorded in the commit message:

- `workspace-node`: **zero** instruction-level differences.
- `workspace-python`: exactly **one** difference — the pip install line gains ` ruff` (`RUN pip install --no-cache-dir pytest pytest-cov playwright` → `... playwright ruff`), the deliberate addition backing python's Phase 23 check command.

Any other instruction difference is a template bug: stop and fix the template, do not adjust the shipped files to match.

- [ ] **Step 3: Sanity-check the new go Dockerfile**

Read `images/workspace-go/Dockerfile` and confirm it contains, in order: `FROM golang:1.22-bookworm`; the apt block with `git curl python3 python3-pip`; `RUN pip install --break-system-packages --no-cache-dir playwright`; the Playwright ENV + install block; `ENV GOCACHE=/home/sandbox/.cache/go-build` and `ENV GOMODCACHE=/home/sandbox/go/pkg/mod`; the setuid-strip RUN; the groupadd/useradd sandbox block; `WORKDIR /workspace`; `USER 1000:1000`.

- [ ] **Step 4: Run the golden test**

Run (host/CI): `python3 -m pytest services/executor/tests/unit/test_imagegen.py -v`
Expected: all PASS including the golden test. In this sandbox, run the same comparison via the hand-built-`StackConfig` script instead and record that the golden test was hand-traced.

- [ ] **Step 5: Commit**

```bash
git add images/workspace-python/Dockerfile images/workspace-node/Dockerfile images/workspace-go/Dockerfile
git commit -m "feat(images): regenerate workspace Dockerfiles from template; add go (ADR-045)"
```

---

### Task 5: Executor config, containers, and test runner read the stack registry

**Files:**
- Modify: `services/executor/config.py`
- Modify: `services/executor/containers.py`
- Modify: `services/executor/tools.py` (`tool_run_tests` gains registry-driven test commands)
- Modify: `services/executor/Dockerfile` (add `"pyyaml>=6.0"` to the pip install list)
- Modify: `services/executor/tests/unit/test_config.py`
- Modify: `services/executor/tests/unit/test_containers_mocked.py`
- Modify: `services/executor/tests/unit/test_tools_mocked.py`
- Create: `services/executor/tests/unit/conftest.py` (shared stacks fixture) — if one already exists, add the fixture to it.

**Interfaces:**
- Consumes: `StacksConfig`/`StackConfig`/`load_stacks_config` from Task 1.
- Produces: `ExecutorConfig.stacks: StacksConfig` (default: empty registry), `DEFAULT_STACKS_CONFIG_PATH = "config/stacks.yaml"`, `image_for_stack(stack, config)` unchanged in signature but registry-driven; `containers.py` loses `CACHE_VOLUME_TARGET_FOR_STACK` and `MANIFEST_INSTALL_COMMANDS_FOR_STACK` entirely; `tool_run_tests` prefers the registry's `test_command` for the project's stack, keeping the `framework` parameter as the fallback for unregistered stacks (no Orchestrator-side change needed — `agents/tester.py`'s `_FRAMEWORK_FOR_STACK` stays untouched because the Executor's registry lookup now wins for every registered stack, go included). Task 6+ relies on `STACKS_CONFIG_PATH` env handling.

- [ ] **Step 1: Add the shared test fixture**

In `services/executor/tests/unit/conftest.py`:

```python
"""Shared fixtures for the Executor's unit tests."""

import pytest

from services.executor.stacks import StackConfig, StackManifest, StacksConfig


@pytest.fixture()
def two_stack_registry() -> StacksConfig:
    """A python+node StacksConfig mirroring config/stacks.yaml's entries."""
    return StacksConfig(
        stacks={
            "python": StackConfig(
                image="foundry/workspace-python:3.12",
                base_image="python:3.12-slim",
                manifests=[
                    StackManifest(
                        file="requirements.txt",
                        install_command=[
                            "pip", "install", "--user", "-r", "requirements.txt",
                        ],
                    ),
                    StackManifest(
                        file="pyproject.toml",
                        install_command=["pip", "install", "--user", "-e", "."],
                    ),
                ],
                cache_dir="/home/sandbox/.cache/pip",
                test_command=[
                    "python3", "-m", "pytest",
                    "--cov=.", "--cov-report=term-missing", "-q",
                ],
            ),
            "node": StackConfig(
                image="foundry/workspace-node:20",
                base_image="node:20-slim",
                sandbox_user_mode="rename",
                rename_from="node",
                manifests=[
                    StackManifest(
                        file="package.json", install_command=["npm", "install"]
                    )
                ],
                cache_dir="/home/sandbox/.npm",
                test_command=["npm", "test"],
            ),
        }
    )
```

- [ ] **Step 2: Write failing tests for the new config surface**

Add to `services/executor/tests/unit/test_config.py` (adapt imports to the file's existing style):

```python
def test_image_for_stack_reads_registry(two_stack_registry):
    config = ExecutorConfig(
        projects_host_path=Path("/srv/projects"), stacks=two_stack_registry
    )
    assert image_for_stack("node", config) == "foundry/workspace-node:20"


def test_image_for_stack_unknown_stack_falls_back_to_default(two_stack_registry):
    config = ExecutorConfig(
        projects_host_path=Path("/srv/projects"), stacks=two_stack_registry
    )
    assert image_for_stack("cobol", config) == config.workspace_image


def test_config_defaults_to_empty_registry():
    config = ExecutorConfig(projects_host_path=Path("/srv/projects"))
    assert config.stacks.stacks == {}


def test_from_env_reads_stacks_config_path(monkeypatch, tmp_path):
    pytest.importorskip("yaml")
    stacks_file = tmp_path / "stacks.yaml"
    stacks_file.write_text(
        "stacks:\n"
        "  python:\n"
        "    image: foundry/workspace-python:3.12\n"
        "    base_image: python:3.12-slim\n"
    )
    monkeypatch.setenv("PROJECTS_HOST_PATH", "/srv/projects")
    monkeypatch.setenv("STACKS_CONFIG_PATH", str(stacks_file))
    config = ExecutorConfig.from_env()
    assert set(config.stacks.stacks) == {"python"}
```

- [ ] **Step 3: Run to verify they fail**

Run: `python3 -m pytest services/executor/tests/unit/test_config.py -v`
Expected: new tests FAIL (`stacks` unknown field / `image_for_stack` reads `STACK_IMAGES`).

- [ ] **Step 4: Rewrite `services/executor/config.py`**

Replace `STACK_IMAGES` and `image_for_stack` and extend `ExecutorConfig`:

```python
"""Executor runtime configuration, sourced from environment variables."""

import os
from pathlib import Path

from pydantic import BaseModel, Field

from services.executor.stacks import StacksConfig, load_stacks_config

DEFAULT_WORKSPACE_IMAGE = "foundry/workspace-python:3.12"
DEFAULT_NETWORK_NAME = "foundry_workspace"
DEFAULT_MEM_LIMIT = "2g"
DEFAULT_NANO_CPUS = 2_000_000_000
DEFAULT_COMMAND_TIMEOUT_SECONDS = 300
DEFAULT_STACKS_CONFIG_PATH = "config/stacks.yaml"


class ExecutorConfig(BaseModel):
    """Runtime configuration for the Executor, validated once at process startup.

    mem_limit and nano_cpus are process-wide defaults for v1; Phase 7's
    projects.yaml is expected to add per-project overrides on top of these.
    `stacks` is the validated stack registry (ADR-045); it defaults to an
    empty registry so tests can construct a config without touching the
    filesystem, while from_env always loads the real config/stacks.yaml.
    """

    projects_host_path: Path
    workspace_image: str = DEFAULT_WORKSPACE_IMAGE
    network_name: str = DEFAULT_NETWORK_NAME
    mem_limit: str = DEFAULT_MEM_LIMIT
    nano_cpus: int = DEFAULT_NANO_CPUS
    command_timeout_seconds: int = DEFAULT_COMMAND_TIMEOUT_SECONDS
    stacks: StacksConfig = Field(default_factory=lambda: StacksConfig(stacks={}))
```

Keep `from_env`'s existing body and docstring, adding before the `cls(...)` call:

```python
        stacks = load_stacks_config(
            Path(os.environ.get("STACKS_CONFIG_PATH", DEFAULT_STACKS_CONFIG_PATH))
        )
```

and `stacks=stacks,` inside `cls(...)`, plus a docstring line documenting `STACKS_CONFIG_PATH` and that a missing stacks.yaml raises `FileNotFoundError` at startup.

Replace `image_for_stack`:

```python
def image_for_stack(stack: str, config: "ExecutorConfig") -> str:
    """Resolves the workspace image for a project's stack (ADR-028, ADR-045).

    Args:
        stack: The project's declared stack (e.g. "python", "node", "go").
        config: The Executor's runtime configuration.

    Returns:
        The registered image for *stack* in config.stacks, otherwise
        config.workspace_image -- so a stack with no registry entry still
        gets a working (if not ideal) container instead of a hard failure.
    """
    entry = config.stacks.stacks.get(stack)
    return entry.image if entry is not None else config.workspace_image
```

- [ ] **Step 5: Rewrite the two hardcoded dicts out of `services/executor/containers.py`**

Delete `CACHE_VOLUME_TARGET_FOR_STACK` and `MANIFEST_INSTALL_COMMANDS_FOR_STACK`. In `workspace_container`, replace the cache lookup:

```python
    stack_entry = config.stacks.stacks.get(stack)
    volumes = {str(project_host_path): {"bind": WORKSPACE_MOUNT_TARGET, "mode": "rw"}}
    if stack_entry is not None and stack_entry.cache_dir is not None:
        volumes[cache_volume_name(project_id)] = {
            "bind": stack_entry.cache_dir, "mode": "rw",
        }
```

and the install call:

```python
        if install_dependencies and stack_entry is not None:
            _install_dependencies_if_present(
                container, stack_entry, config.command_timeout_seconds
            )
```

Change `_install_dependencies_if_present` to take the registry entry instead of a stack name:

```python
def _install_dependencies_if_present(
    container: Container, stack_entry: StackConfig, timeout: int
) -> None:
```

with the loop body iterating `stack_entry.manifests` (`manifest.file` / `manifest.install_command`); keep the existing docstring, updating the source-of-truth reference from `MANIFEST_INSTALL_COMMANDS_FOR_STACK` to `config/stacks.yaml (ADR-045)`. In `clear_project_cache_volume`, replace the dict lookup with `config.stacks.stacks.get(stack)` and its `cache_dir` (early-return when the entry or its `cache_dir` is `None`). Update the module import to `from services.executor.stacks import StackConfig` and the module docstring's Phase 17 sentence to mention stacks.yaml.

- [ ] **Step 6: `tool_run_tests` prefers the registry's test_command**

In `services/executor/tools.py`, replace `tool_run_tests`'s command selection:

```python
    entry = config.stacks.stacks.get(stack)
    if entry is not None and entry.test_command is not None:
        command = entry.test_command
    elif framework == "pytest":
        command = ["python3", "-m", "pytest", "--cov=.", "--cov-report=term-missing", "-q"]
    elif framework == "npm":
        command = ["npm", "test"]
    else:
        raise ValueError(f"unsupported test framework: {framework!r}")
```

and make coverage parsing opportunistic instead of framework-gated: always call `_parse_coverage_pct(combined_output)` (it returns `None` when no coverage.py `TOTAL` line matches, so npm/go output yields `None` exactly as before). Update the docstring: the registry's `test_command` wins for registered stacks (ADR-045); `framework` remains the legacy fallback for stacks absent from the registry. Add unit tests to `test_tools_mocked.py` (using the mocked-container pattern that file already has): a go-stack `tool_run_tests` call — build a one-stack `StacksConfig` inline with `test_command=["go", "test", "./..."]` — executes exactly that command, and an unregistered stack still falls back to the `framework` behavior.

- [ ] **Step 7: Update existing unit tests and run the whole executor unit suite**

`test_containers_mocked.py` and `test_tools_mocked.py` reference the deleted dicts and/or construct `ExecutorConfig` expecting python/node behavior. Mechanical update: give those tests the `two_stack_registry` fixture and pass `stacks=two_stack_registry` into every `ExecutorConfig(...)` construction whose assertions depend on cache volumes, install commands, or per-stack images; leave constructions that don't care untouched (empty registry now means "no cache volume, no installs, default image", which is also worth one new assertion in `test_containers_mocked.py`: unknown stack ⇒ only the project bind mount).

Run: `python3 -m pytest services/executor/tests/unit -v`
Expected: all PASS (yaml-dependent tests SKIP in-sandbox).

- [ ] **Step 8: Add pyyaml to the Executor image**

In `services/executor/Dockerfile`, extend the pip install list with `"pyyaml>=6.0"` (version floor mirrors `pyproject.toml`).

- [ ] **Step 9: Commit**

```bash
git add services/executor/config.py services/executor/containers.py services/executor/Dockerfile services/executor/tests/unit/
git commit -m "feat(executor): stack registry drives images, caches, installs (ADR-045)"
```

---

### Task 6: Compose wiring for the Executor's stacks.yaml

**Files:**
- Modify: `docker-compose.yml`

**Interfaces:**
- Consumes: `STACKS_CONFIG_PATH` handling from Task 5.
- Produces: both services see `/app/config/stacks.yaml` at runtime.

- [ ] **Step 1: Edit `docker-compose.yml`**

In the `executor` service: add `STACKS_CONFIG_PATH: /app/config/stacks.yaml` to `environment` and a new `volumes` list with `- ./config:/app/config:ro`. In the `orchestrator` service (which already mounts `./config`): add `STACKS_CONFIG_PATH: /app/config/stacks.yaml` to `environment`.

- [ ] **Step 2: Verify the security boundary is untouched**

Run: `git diff docker-compose.yml`
Expected: the `docker-socket-proxy` service block shows **zero changed lines** (this is the phase's Definition of Done). If any line in that block changed, revert.

- [ ] **Step 3: Commit**

```bash
git add docker-compose.yml
git commit -m "chore(infra): mount stacks.yaml into executor and orchestrator"
```

---

### Task 7: Orchestrator loader copy + registry/CLI/state validation from the registry

**Files:**
- Create: `services/orchestrator/stacks.py`
- Test: `services/orchestrator/tests/unit/test_stacks.py`
- Modify: `services/orchestrator/registry/schemas.py`
- Modify: `services/orchestrator/registry/cli.py`
- Modify: `services/orchestrator/graph/state.py`
- Modify: `services/orchestrator/registry/tests/unit/test_cli.py`

**Interfaces:**
- Consumes: nothing from other tasks (independent code path).
- Produces: `services/orchestrator/stacks.py` exporting the same `StackManifest`/`StackCheck`/`StackConfig`/`StacksConfig`/`load_stacks_config` names as Task 1 (Phase 23's `node_static_checks` imports `StacksConfig` from **this** module); `registry.cli.build_parser(stack_choices: Sequence[str])` (signature change); `Stack = str` in both `registry/schemas.py` and `graph/state.py`.

- [ ] **Step 1: Create `services/orchestrator/stacks.py`**

Copy `services/executor/stacks.py` verbatim except the module docstring, which becomes:

```python
"""Pydantic schema and loader for config/stacks.yaml (ADR-045) -- Orchestrator copy.

A deliberate duplicate of services/executor/stacks.py rather than a
cross-service import: the two services communicate over HTTP and never
import each other's packages (the same decoupling executor_client.py's
duplicated result schemas already follow). Both copies validate the same
file at startup, so schema drift between them fails fast rather than
silently. The Orchestrator's consumers are the registry CLI's --stack
choices and Phase 23's static-checks node (check_commands).
"""
```

Note on the "validated at Orchestrator startup" TODO bullet: this task gives the Orchestrator its loader and its first load site (the registry CLI); the `main.py` startup load lands in Phase 23 Task 3 together with the graph threading that first consumes it — record that split in the TODO verification note rather than adding a load-with-no-consumer here.

- [ ] **Step 2: Write `services/orchestrator/tests/unit/test_stacks.py`**

```python
"""Unit tests for the Orchestrator's stacks.yaml loader copy (ADR-045)."""

from pathlib import Path

import pytest

from services.orchestrator.stacks import StackConfig, StacksConfig, load_stacks_config


def test_stack_config_minimal_validates():
    stack = StackConfig.model_validate(
        {"image": "foundry/workspace-go:1.22", "base_image": "golang:1.22-bookworm"}
    )
    assert stack.check_commands == []


def test_loader_matches_executor_copy_schema(tmp_path: Path):
    pytest.importorskip("yaml")
    config_file = tmp_path / "stacks.yaml"
    config_file.write_text(
        "stacks:\n"
        "  go:\n"
        "    image: foundry/workspace-go:1.22\n"
        "    base_image: golang:1.22-bookworm\n"
        "    check_commands:\n"
        "      - command: [go, vet, ./...]\n"
    )
    loaded = load_stacks_config(config_file)
    assert loaded.stacks["go"].check_commands[0].command == ["go", "vet", "./..."]


def test_both_copies_expose_identical_field_sets():
    from services.executor import stacks as executor_stacks

    assert set(StackConfig.model_fields) == set(
        executor_stacks.StackConfig.model_fields
    )
    assert set(StacksConfig.model_fields) == set(
        executor_stacks.StacksConfig.model_fields
    )
```

Run: `python3 -m pytest services/orchestrator/tests/unit/test_stacks.py -v` — FAIL first (module missing), PASS after Step 1's file exists.

- [ ] **Step 3: Widen the two `Stack` Literals**

In `services/orchestrator/registry/schemas.py` replace `Stack = Literal["python", "node"]` with:

```python
Stack = str
```

and drop `Literal` from the import if now unused there. Add one docstring line on `ProjectRecord`: valid stack values are the keys of `config/stacks.yaml`, enforced at the CLI boundary (`registry/cli.py`), not by this schema.

In `services/orchestrator/graph/state.py` make the same replacement for its `Stack = Literal["python", "node"]`, with the same one-line docstring note on `ProjectState`.

- [ ] **Step 4: Registry CLI takes its choices from the registry file**

In `services/orchestrator/registry/cli.py`:

```python
def build_parser(stack_choices: Sequence[str]) -> argparse.ArgumentParser:
    """Builds the `new-project` argument parser.

    Args:
        stack_choices: Valid --stack values -- the keys of config/stacks.yaml,
            loaded by main() (ADR-045). Passed in rather than loaded here so
            the parser stays constructible in tests without pyyaml.
    """
```

with `parser.add_argument("--stack", required=True, choices=sorted(stack_choices))`. In `main()`, before parsing:

```python
    from services.orchestrator.stacks import load_stacks_config

    stacks_path = Path(os.environ.get("STACKS_CONFIG_PATH", "config/stacks.yaml"))
    stack_choices = sorted(load_stacks_config(stacks_path).stacks)
    args = build_parser(stack_choices).parse_args(argv)
```

(lazy import inside `main`, matching `_start_intake`'s documented pattern). Add `from collections.abc import Sequence` to the module's imports.

- [ ] **Step 5: Update CLI tests and run the registry + orchestrator unit suites**

In `services/orchestrator/registry/tests/unit/test_cli.py`, update every `build_parser()` call to `build_parser(["python", "node", "go"])` and add:

```python
def test_build_parser_rejects_unregistered_stack():
    parser = build_parser(["python", "node"])
    with pytest.raises(SystemExit):
        parser.parse_args(["proj1", "--name", "P", "--stack", "go"])


def test_build_parser_accepts_registered_stack():
    parser = build_parser(["python", "node", "go"])
    args = parser.parse_args(["proj1", "--name", "P", "--stack", "go"])
    assert args.stack == "go"
```

Run: `python3 -m pytest services/orchestrator/registry/tests/unit services/orchestrator/tests/unit -v`
Expected: all PASS (yaml-marked tests SKIP in-sandbox).

- [ ] **Step 6: Commit**

```bash
git add services/orchestrator/stacks.py services/orchestrator/tests/unit/test_stacks.py services/orchestrator/registry/ services/orchestrator/graph/state.py
git commit -m "feat(orchestrator): stack validation sourced from stacks.yaml (ADR-045)"
```

---

### Task 8: Go round-trip integration test (real Docker)

**Files:**
- Create: `services/executor/tests/integration/test_go_stack.py`

**Interfaces:**
- Consumes: everything above plus a built `foundry/workspace-go:1.22` image and a real Docker daemon (host only — cannot run in this sandbox).

- [ ] **Step 1: Write the test**

Follow `test_executor_sandbox.py`'s existing preamble pattern: the client comes from `services.executor.docker_client.build_docker_client()` (never a direct SDK import — the guard hook forbids it outside `services/executor/`, and this file is inside, but the shared helper also carries the DOCKER_HOST handling), wrapped in a try/except that `pytest.skip`s when no daemon is reachable.

```python
"""Real-Docker integration test: a Go project round-trips on the generated image.

Phase 22's Definition of Done (ADR-045): dependency install resolves a real
module through the go stack's cache volume and the project builds and runs
inside the template-generated foundry/workspace-go image. Requires a Docker
daemon, the built image, and network egress from the foundry_workspace
network; skipped automatically when either is absent.
"""

from pathlib import Path

import pytest

from services.executor.config import ExecutorConfig
from services.executor.docker_client import build_docker_client
from services.executor.stacks import load_stacks_config
from services.executor.tools import tool_run_command, tool_run_tests, tool_write_file

GO_MOD = """module foundrytest

go 1.22

require rsc.io/quote v1.5.2
"""

MAIN_GO = """package main

import (
	"fmt"

	"rsc.io/quote"
)

func Greeting() string {
	return quote.Hello()
}

func main() {
	fmt.Println(Greeting())
}
"""

MAIN_TEST_GO = """package main

import "testing"

func TestGreetingIsNotEmpty(t *testing.T) {
	if Greeting() == "" {
		t.Fatal("empty greeting")
	}
}
"""


@pytest.mark.integration
def test_go_project_installs_dependency_and_builds(tmp_path):
    pytest.importorskip("yaml")
    try:
        client = build_docker_client()
        client.ping()
    except Exception as exc:
        pytest.skip(f"no reachable Docker daemon: {exc}")
    try:
        client.images.get("foundry/workspace-go:1.22")
    except Exception:
        pytest.skip("foundry/workspace-go:1.22 not built on this host")

    project_id = "go-int-test"
    (tmp_path / project_id).mkdir()
    config = ExecutorConfig(
        projects_host_path=tmp_path,
        stacks=load_stacks_config(Path("config/stacks.yaml")),
    )
    tool_write_file(client, config, project_id, "go.mod", GO_MOD, stack="go")
    tool_write_file(client, config, project_id, "main.go", MAIN_GO, stack="go")
    tool_write_file(client, config, project_id, "main_test.go", MAIN_TEST_GO, stack="go")

    build = tool_run_command(
        client, config, project_id, ["go", "build", "./..."], stack="go"
    )
    assert build.exit_code == 0, build.stderr

    run = tool_run_command(client, config, project_id, ["go", "run", "."], stack="go")
    assert run.exit_code == 0, run.stderr
    assert "Hello" in run.stdout

    tests = tool_run_tests(client, config, project_id, stack="go")
    assert "ok" in tests.report
    assert "FAIL" not in tests.report
```

Note the `except Exception` uses here are specific enough for a skip-guard but still log-free; if the repo's ruff config flags them, narrow to `docker.errors.DockerException`-equivalents via the errors already re-exported through `services.executor.errors`.

- [ ] **Step 2: Verify by hand-trace in-sandbox; run for real on the host**

Hand-trace: `tool_run_command(..., stack="go")` → `workspace_container` resolves `image_for_stack("go")` = `foundry/workspace-go:1.22` from the registry, mounts `foundry-cache-go-int-test` at `/home/sandbox/go/pkg/mod`, `install_dependencies=True` finds `go.mod` and runs `go mod download` before the build command. On the host later:

```bash
python3 scripts/gen_workspace_images.py
docker build -t foundry/workspace-go:1.22 images/workspace-go/
python3 -m pytest -m integration services/executor/tests/integration/test_go_stack.py -v
```

Expected: PASS (or SKIP if the image isn't built). Record in the commit message whether this ran for real or was hand-traced.

- [ ] **Step 3: Commit**

```bash
git add services/executor/tests/integration/test_go_stack.py
git commit -m "test(executor): go-stack round-trip integration test (ADR-045)"
```

---

### Task 9: Phase close-out

**Files:**
- Modify: `TODO.md` (Phase 22 checkboxes + verification note)
- Modify: `ARCHITECTURE.md` (§4.3: one short paragraph noting workspace Dockerfiles are now generated from `images/workspace.template.Dockerfile` via `config/stacks.yaml`; ADR-045 already exists — do not add a new ADR)
- Modify: `CLAUDE.md` (Commands section: add `docker build -t foundry/workspace-go:1.22 images/workspace-go/` beside the other two builds, and a line for `python3 scripts/gen_workspace_images.py`)

- [ ] **Step 1: Run everything runnable**

```bash
python3 -m pytest services/executor/tests/unit services/orchestrator/registry/tests/unit services/orchestrator/tests/unit -q
```

Expected: all PASS/SKIP, zero FAIL. `ruff check` is unavailable in-sandbox — note that in the phase commit message; CI runs it.

- [ ] **Step 2: Check Phase 22's boxes in TODO.md** with a short verification note per line (what ran vs what was hand-traced), matching the style of earlier phases.

- [ ] **Step 3: Update ARCHITECTURE.md §4.3 and CLAUDE.md as above.**

- [ ] **Step 4: Phase-end commit and tag**

```bash
git add TODO.md ARCHITECTURE.md CLAUDE.md
git commit -m "Phase 22: stack registry and templated workspace images"
git tag phase-22-done
```
