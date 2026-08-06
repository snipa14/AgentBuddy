# Dynamic Skills & Plugins Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Foundry's LangGraph runtime actually load and use the `SKILL.md` files that already exist (closing the ADR-014 gap), and let an operator drop new agent roles into a `plugins/` directory that gets auto-discovered at Orchestrator startup and wired into the graph at one of three fixed extension slots — no code change required.

**Architecture:** A new `skills/loader.py` reads `SKILL.md` frontmatter+body and is called by every LLM-backed built-in agent module instead of each hand-rolling its own prompt string. A new `services/orchestrator/plugins/` package discovers `plugins/*/plugin.yaml` manifests at startup, validates them, and (via a new `services/orchestrator/graph/plugin_node.py`) wires one generic node per surviving plugin into `graph/build.py` at its declared slot, chained through the existing `add_conditional_edges` dict-remapping mechanism — no changes to the existing routing functions' own logic.

**Tech Stack:** Python 3.12, Pydantic v2, LangGraph `StateGraph`, LangChain `with_structured_output`, pytest.

## Global Constraints

- No inline comments; comprehensive Google-style docstrings on every public function/class (CLAUDE.md).
- `snake_case` functions/variables, `PascalCase` classes, node functions prefixed `node_`, private module helpers prefixed `_` (CLAUDE.md).
- No bare `except`; always catch a specific type or `Exception` with a log (CLAUDE.md).
- Pydantic for all external data — every config file this plan introduces is validated through a Pydantic model before use (CLAUDE.md).
- Type hints everywhere; no `Any` without a comment explaining why (CLAUDE.md).
- Never reference a model name string directly in agent code — plugin roles go through `model_router.resolve(role)` exactly like built-in roles (CLAUDE.md).
- Never hardcode a credential (CLAUDE.md) — not applicable to this plan's files, but any fixture must not contain anything that looks like a key/token/secret.
- **Permanent sandbox gap** (see `project-foundry-phase-workflow` memory): `langgraph`, `langchain_core`, `langchain_anthropic`, `langchain_ollama`, `pyyaml`, and `ruff` are not installed in this sandbox, and host installs are blocked by this repo's own `block_host_installs.py` pre-tool-use hook. Every task below states explicitly which of its steps can run for real here (`pytest ...`) versus must be hand-traced (anything importing `langchain_core`, `langgraph`, or `yaml`). Do not attempt `pip install` to fix this — it is expected, not a bug.
- Test file layout follows `.claude/rules/testing.md`: `tests/unit/` (no real Docker/network/langgraph-dependent I/O where avoidable) and `tests/integration/` (marked `@pytest.mark.integration`), mirroring each existing `services/orchestrator/<subpackage>/tests/unit/` pattern.
- Every new subpackage/tests directory needs an `__init__.py` (matches `services/orchestrator/registry/tests/unit/__init__.py`, `services/orchestrator/agents/tests/unit/__init__.py`).
- LLM-backed agent tests mock the provider at the `model_router.resolve()` boundary only, per `.claude/rules/testing.md` — reuse `services/orchestrator/agents/tests/unit/router_fakes.py`'s `FakeRouter`/`structured()`, don't invent a parallel fake.
- A file that needs `pyyaml` to load real files imports `yaml` lazily inside the function body (not at module top level), matching `services/orchestrator/stacks.py::load_stacks_config`'s existing pattern, and its real-file test guards with `pytest.importorskip("yaml")`, matching `services/orchestrator/tests/unit/test_stacks.py`.

---

### Task 1: `skills/loader.py` — read `SKILL.md` files by name

**Files:**
- Create: `skills/loader.py`
- Create: `skills/tests/__init__.py`, `skills/tests/unit/__init__.py`
- Test: `skills/tests/unit/test_loader.py`

**Interfaces:**
- Produces: `SkillContent` (dataclass: `name: str`, `description: str`, `body: str`), `load_skill(name: str) -> SkillContent` (cached, raises `FileNotFoundError`/`ValueError`), `load_skill_from_path(path: pathlib.Path) -> SkillContent` (uncached, same parsing, used by plugin discovery in Task 4 for `SKILL.md` files that don't live under `skills/`).

Every observed `SKILL.md` frontmatter in this repo is flat `key: value` scalars only (verified against all 7 packaged skills plus the superpowers plugin's own skills) — some values plain, some double-quoted. No nested lists/dicts appear. A minimal hand-written parser (strip one matching pair of outer double-quotes per value) covers this without a `pyyaml` dependency, keeping this module's tests fully real in this sandbox.

- [ ] **Step 1: Write the failing tests**

```python
# skills/tests/unit/test_loader.py
"""Unit tests for skills/loader.py."""

from pathlib import Path

import pytest

from skills.loader import SkillContent, load_skill, load_skill_from_path


def test_load_skill_reads_existing_skill():
    content = load_skill("develop-python")
    assert content.name == "develop-python"
    assert "Implements a single planned task" in content.description
    assert content.body.startswith("# Develop Python")


def test_load_skill_caches_result():
    first = load_skill("review-code")
    second = load_skill("review-code")
    assert first is second


def test_load_skill_raises_file_not_found_for_unknown_skill():
    with pytest.raises(FileNotFoundError):
        load_skill("does-not-exist")


def test_load_skill_strips_quoted_description():
    content = load_skill("frontend-review")
    assert not content.description.startswith('"')
    assert not content.description.endswith('"')


def test_load_skill_from_path_parses_arbitrary_file(tmp_path: Path):
    skill_file = tmp_path / "SKILL.md"
    skill_file.write_text(
        '---\nname: my-plugin-skill\ndescription: "A plugin skill"\n---\n\n'
        "# My Plugin Skill\n\nDo the thing.\n"
    )
    content = load_skill_from_path(skill_file)
    assert content.name == "my-plugin-skill"
    assert content.description == "A plugin skill"
    assert content.body == "# My Plugin Skill\n\nDo the thing.\n"


def test_parse_rejects_missing_frontmatter(tmp_path: Path):
    skill_file = tmp_path / "SKILL.md"
    skill_file.write_text("# No frontmatter here\n")
    with pytest.raises(ValueError, match="frontmatter"):
        load_skill_from_path(skill_file)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest skills/tests/unit/test_loader.py -v`
Expected: `ModuleNotFoundError: No module named 'skills.loader'`

- [ ] **Step 3: Write the implementation**

```python
# skills/loader.py
"""Loads Agent-Skills-format SKILL.md files by name (ARCHITECTURE.md §4.8, ADR-014).

Every packaged skill's frontmatter is flat scalar key: value pairs (no
nested lists/dicts) -- verified against every SKILL.md this repo and the
superpowers plugin ship. A minimal hand-written parser covers this without
a pyyaml dependency, which matters because pyyaml is not guaranteed to be
installed in every environment this module is imported from.
"""

import functools
from dataclasses import dataclass
from pathlib import Path

SKILLS_ROOT = Path(__file__).parent


@dataclass(frozen=True)
class SkillContent:
    """A parsed SKILL.md file.

    Attributes:
        name: The skill's declared name from its frontmatter.
        description: The skill's declared description from its frontmatter.
        body: Everything after the frontmatter's closing '---' line.
    """

    name: str
    description: str
    body: str


def _strip_matching_quotes(value: str) -> str:
    """Strips one pair of matching outer double-quotes from a frontmatter value."""
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        return value[1:-1]
    return value


def _parse_skill_md(text: str) -> tuple[dict[str, str], str]:
    """Splits a SKILL.md file's text into its frontmatter dict and body.

    Args:
        text: The full contents of a SKILL.md file.

    Returns:
        A tuple of (frontmatter dict, body text).

    Raises:
        ValueError: If the text has no valid '---'-delimited frontmatter block.
    """
    if not text.startswith("---\n"):
        raise ValueError("SKILL.md must start with a '---' frontmatter block")
    end = text.find("\n---\n", 4)
    if end == -1:
        raise ValueError("SKILL.md frontmatter block has no closing '---'")
    frontmatter_text = text[4:end]
    body = text[end + 5 :].lstrip("\n")
    frontmatter: dict[str, str] = {}
    for line in frontmatter_text.splitlines():
        if not line.strip():
            continue
        key, _, value = line.partition(":")
        frontmatter[key.strip()] = _strip_matching_quotes(value.strip())
    return frontmatter, body


def load_skill_from_path(path: Path) -> SkillContent:
    """Loads and parses a SKILL.md file from an arbitrary path.

    Args:
        path: Path to a SKILL.md file.

    Returns:
        The parsed skill content. `name`/`description` fall back to the
        parent directory's name / an empty string if the frontmatter omits
        them.

    Raises:
        FileNotFoundError: If path does not exist.
        ValueError: If the file has no valid frontmatter block.
    """
    frontmatter, body = _parse_skill_md(path.read_text())
    return SkillContent(
        name=frontmatter.get("name", path.parent.name),
        description=frontmatter.get("description", ""),
        body=body,
    )


@functools.lru_cache(maxsize=None)
def load_skill(name: str) -> SkillContent:
    """Loads and caches `skills/<name>/SKILL.md`.

    Args:
        name: The skill's directory name under `skills/`.

    Returns:
        The parsed skill content, cached for the process lifetime -- skill
        files are static for the duration of a single deployment run.

    Raises:
        FileNotFoundError: If `skills/<name>/SKILL.md` doesn't exist.
        ValueError: If the file has no valid frontmatter block.
    """
    return load_skill_from_path(SKILLS_ROOT / name / "SKILL.md")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest skills/tests/unit/test_loader.py -v`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add skills/loader.py skills/tests/__init__.py skills/tests/unit/__init__.py skills/tests/unit/test_loader.py
git commit -m "feat(skills): add SKILL.md loader (closes ADR-014's dynamic-loading gap)"
```

---

### Task 2: Wire the loader into the four built-in LLM-backed agents

**Files:**
- Modify: `services/orchestrator/agents/planner.py` (the `_CRITIQUE_SYSTEM_PROMPT` constant and its one use in `critique_plan`)
- Modify: `services/orchestrator/agents/reviewer.py` (the `_REVIEW_SYSTEM_PROMPT` constant and its one use in `review_changes`)
- Modify: `services/orchestrator/agents/frontend_reviewer.py` (the `_FRONTEND_SYSTEM_PROMPT` constant and its one use)
- Modify: `services/orchestrator/agents/coder.py` (the `_CODER_SYSTEM_PROMPT` constant and its one use; `_DECOMPOSE_SYSTEM_PROMPT` is untouched — task decomposition has no corresponding packaged skill)

**Interfaces:**
- Consumes: `skills.loader.load_skill(name: str) -> SkillContent` (Task 1).
- Produces: no new public interface; each module's existing public functions (`critique_plan`, `review_changes`, `review_frontend`, `run_coder_task`) keep their exact signatures.

This is the same three-line edit repeated four times: replace a hardcoded prompt constant with a small function that combines the loaded skill's `body` with a short Foundry-owned preface stating how the input actually arrives (something no portable skill file should hardcode, since ADR-014's whole point is that the skill body works unmodified outside Foundry too).

**Sandbox note:** every file this task touches imports `langchain_core`, which is not installed here (Global Constraints). Steps 1/2/4 below are written as real pytest but will fail at collection with `ModuleNotFoundError: No module named 'langchain_core'` in this sandbox — run them for real on a machine with the project's dependencies installed; verify correctness here by hand-trace instead, same as every prior phase's agent-layer LangChain-dependent work.

- [ ] **Step 1: Write the failing/updated tests**

Add one assertion to each of the four existing test files proving the skill body is actually reaching the model, by inspecting the recorded system message on the fake model.

```python
# services/orchestrator/agents/tests/unit/test_reviewer.py -- add this test
def test_review_changes_system_prompt_includes_skill_body():
    router = FakeRouter([structured(APPROVE_RESULT)])
    executor = RecordingExecutorClient(files={"models.py": "class User: pass\n"})

    asyncio.run(review_changes(router, executor, "proj-1"))

    system_message = router.model.requests[0][0]
    assert "Correctness bugs" in system_message.content  # from skills/review-code/SKILL.md
```

```python
# services/orchestrator/agents/tests/unit/test_frontend_reviewer.py -- add this test
def test_review_frontend_system_prompt_includes_skill_body():
    # A real screenshot (not an error result) is required so review_frontend
    # actually reaches the LLM call -- test_review_frontend_approves_when_no_screenshots'
    # all-failed-screenshots case short-circuits before ever calling the model.
    executor = RecordingExecutorClient(
        files=["index.html"],
        screenshots=[ScreenshotResult(image_b64="iVBORw0KGgo=", error=None)],
    )
    router = FakeRouter([structured(APPROVE_RESULT)])

    asyncio.run(review_frontend(router, executor, "proj-1"))

    system_message = router.model.requests[0][0]
    assert "Layout breakage" in system_message.content  # from skills/frontend-review/SKILL.md
```

```python
# services/orchestrator/agents/tests/unit/test_planner.py -- add this test
def test_critique_plan_system_prompt_includes_skill_body():
    router = FakeRouter([structured(CRITIQUE_NO_FINDINGS)])
    plan = DraftPlan(summary="s")

    asyncio.run(critique_plan(router, plan))

    system_message = router.model.requests[0][0]
    assert "Unstated assumptions" in system_message.content  # from skills/roast-the-plan/SKILL.md
```

```python
# services/orchestrator/agents/tests/unit/test_coder.py -- add this test
def test_run_coder_task_system_prompt_includes_skill_body():
    router = FakeRouter([_done_message()])  # this file's existing no-tool-calls fixture, line ~213
    executor = RecordingExecutorClient()

    asyncio.run(run_coder_task(router, executor, "proj-1", "add a function"))

    system_message = router.model.requests[0][0]
    assert "Match the task, not more" in system_message.content  # from skills/develop-python/SKILL.md
```

- [ ] **Step 2: Run the four new tests to verify they fail**

Run: `pytest services/orchestrator/agents/tests/unit/test_reviewer.py services/orchestrator/agents/tests/unit/test_frontend_reviewer.py services/orchestrator/agents/tests/unit/test_planner.py services/orchestrator/agents/tests/unit/test_coder.py -v`
Expected (on a machine with `langchain_core` installed): 4 new tests FAIL — the old hardcoded prompt text doesn't contain the skill's checklist wording. In this sandbox: collection fails with `ModuleNotFoundError: No module named 'langchain_core'` — hand-trace instead.

- [ ] **Step 3: Write the implementation**

`reviewer.py`:

```python
# replace the _REVIEW_SYSTEM_PROMPT constant with:
from skills.loader import load_skill

_REVIEW_HARNESS_PREFACE = (
    "You will receive the contents of files that were just written by a "
    "Coder agent inside a workspace container."
)


def _build_review_system_prompt() -> str:
    return f"{load_skill('review-code').body}\n\n{_REVIEW_HARNESS_PREFACE}"
```

```python
# in review_changes(), replace:
#     SystemMessage(content=_REVIEW_SYSTEM_PROMPT),
# with:
        SystemMessage(content=_build_review_system_prompt()),
```

`frontend_reviewer.py`:

```python
from skills.loader import load_skill

_FRONTEND_HARNESS_PREFACE = (
    "You will receive screenshots of a web application that was just built "
    "by a Coder agent inside a workspace container."
)


def _build_frontend_system_prompt() -> str:
    return f"{load_skill('frontend-review').body}\n\n{_FRONTEND_HARNESS_PREFACE}"
```

Replace `SystemMessage(content=_FRONTEND_SYSTEM_PROMPT)` with `SystemMessage(content=_build_frontend_system_prompt())`.

`planner.py`:

```python
from skills.loader import load_skill

# delete _CRITIQUE_SYSTEM_PROMPT entirely; in critique_plan(), replace:
#     SystemMessage(content=_CRITIQUE_SYSTEM_PROMPT),
# with:
        SystemMessage(content=load_skill("roast-the-plan").body),
```

`coder.py`:

```python
from skills.loader import load_skill

_CODER_HARNESS_PREFACE = (
    "Your tools are read_file, write_file, and run_command. Once the task "
    "is fully implemented and verified, respond with no further tool calls."
)


def _build_coder_system_prompt() -> str:
    return f"{load_skill('develop-python').body}\n\n{_CODER_HARNESS_PREFACE}"
```

Replace `SystemMessage(content=_CODER_SYSTEM_PROMPT)` with `SystemMessage(content=_build_coder_system_prompt())`; leave `_DECOMPOSE_SYSTEM_PROMPT` and its use untouched.

- [ ] **Step 4: Run the full agents test suite to verify it passes**

Run: `pytest services/orchestrator/agents/tests/unit -v`
Expected (dependencies installed): all pass, including the 4 new tests. In this sandbox: hand-trace each of the four edits against the skill files read during Task 1/planning (confirm the exact substrings asserted in Step 1 really appear in each skill's body — `"Correctness bugs"` is `review-code/SKILL.md`'s checklist item 1 heading, `"Layout breakage"` is `frontend-review/SKILL.md`'s item 1, `"Unstated assumptions"` is `roast-the-plan/SKILL.md`'s item 1, `"Match the task, not more"` is `develop-python/SKILL.md`'s item 1 — all confirmed present verbatim in the files read while writing this plan).

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/agents/planner.py services/orchestrator/agents/reviewer.py services/orchestrator/agents/frontend_reviewer.py services/orchestrator/agents/coder.py services/orchestrator/agents/tests/unit/test_planner.py services/orchestrator/agents/tests/unit/test_reviewer.py services/orchestrator/agents/tests/unit/test_frontend_reviewer.py services/orchestrator/agents/tests/unit/test_coder.py
git commit -m "feat(orchestrator): load built-in agent prompts from skills/*/SKILL.md"
```

---

### Task 3: `services/orchestrator/plugins/manifest.py` — the plugin manifest schema

**Files:**
- Create: `services/orchestrator/plugins/__init__.py` (docstring-only, no re-exports — same rule ADR-030 already established for `agents/__init__.py`, to avoid forcing every submodule import to drag in this package's eventual `langgraph`-dependent siblings)
- Create: `services/orchestrator/plugins/manifest.py`
- Create: `services/orchestrator/plugins/tests/__init__.py`, `services/orchestrator/plugins/tests/unit/__init__.py`
- Test: `services/orchestrator/plugins/tests/unit/test_manifest.py`

**Interfaces:**
- Produces: `PluginManifest` (Pydantic `BaseModel`: `name: str`, `role: str`, `slot: Literal["after_coder", "after_reviewer", "after_tester"]`, `mode: Literal["gating", "advisory"]`, `requires: list[Capability]`, `skill: str = "SKILL.md"`).

This module has no `langchain_core`/`langgraph`/`yaml` dependency at all — it only needs `pydantic` and `services.orchestrator.model_router.capabilities.Capability` (a plain `str` `Enum`, no LangChain dependency). Every step below is real, runnable pytest in this sandbox.

- [ ] **Step 1: Write the failing tests**

```python
# services/orchestrator/plugins/tests/unit/test_manifest.py
"""Unit tests for the plugin manifest schema."""

import pytest
from pydantic import ValidationError

from services.orchestrator.model_router.capabilities import Capability
from services.orchestrator.plugins.manifest import PluginManifest


def test_minimal_manifest_validates_with_default_skill_path():
    manifest = PluginManifest.model_validate(
        {
            "name": "security-audit-reviewer",
            "role": "security_audit_reviewer",
            "slot": "after_reviewer",
            "mode": "gating",
            "requires": ["tool_calling"],
        }
    )
    assert manifest.skill == "SKILL.md"
    assert manifest.requires == [Capability.TOOL_CALLING]


def test_manifest_rejects_unknown_slot():
    with pytest.raises(ValidationError):
        PluginManifest.model_validate(
            {
                "name": "x", "role": "x", "slot": "before_planner",
                "mode": "gating", "requires": [],
            }
        )


def test_manifest_rejects_unknown_mode():
    with pytest.raises(ValidationError):
        PluginManifest.model_validate(
            {
                "name": "x", "role": "x", "slot": "after_coder",
                "mode": "silent", "requires": [],
            }
        )


def test_manifest_rejects_non_snake_case_role():
    with pytest.raises(ValidationError, match="role"):
        PluginManifest.model_validate(
            {
                "name": "x", "role": "Not-Snake-Case", "slot": "after_coder",
                "mode": "gating", "requires": [],
            }
        )


def test_manifest_rejects_uppercase_name():
    with pytest.raises(ValidationError, match="name"):
        PluginManifest.model_validate(
            {
                "name": "NotKebabCase", "role": "x", "slot": "after_coder",
                "mode": "gating", "requires": [],
            }
        )


def test_manifest_accepts_explicit_skill_path():
    manifest = PluginManifest.model_validate(
        {
            "name": "x", "role": "x", "slot": "after_tester",
            "mode": "advisory", "requires": [], "skill": "skill-body.md",
        }
    )
    assert manifest.skill == "skill-body.md"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest services/orchestrator/plugins/tests/unit/test_manifest.py -v`
Expected: `ModuleNotFoundError: No module named 'services.orchestrator.plugins'`

- [ ] **Step 3: Write the implementation**

```python
# services/orchestrator/plugins/__init__.py
"""Discovery and graph-wiring for operator-installed plugins (2026-08-06 design)."""
```

```python
# services/orchestrator/plugins/manifest.py
"""Pydantic schema for a plugin's plugin.yaml manifest.

A Foundry-specific format (not a mirror of Claude Code's .claude-plugin/
plugin.json -- that schema carries hooks/mcpServers/commands fields this
repo's plugin mechanism doesn't act on; see
docs/superpowers/specs/2026-08-06-dynamic-skills-and-plugins-design.md).
"""

import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from services.orchestrator.model_router.capabilities import Capability

Slot = Literal["after_coder", "after_reviewer", "after_tester"]
Mode = Literal["gating", "advisory"]

_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9-]*$")
_ROLE_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")


class PluginManifest(BaseModel):
    """One plugin's declared identity, graph attachment point, and model needs.

    Attributes:
        name: The plugin's kebab-case identifier, used in log messages and
            as the plugin node's chain position. Independent of its
            directory name (the loader also enforces they match, Task 4).
        role: The snake_case model_router role name this plugin resolves
            through -- must not collide with a built-in role or another
            plugin's role (enforced by the loader, Task 4).
        slot: Which fixed extension point this plugin's node attaches to.
        mode: 'gating' lets a changes-requested verdict reopen the most
            recently completed task for another Coder attempt (same
            contract static_checks/ADR-046 uses); 'advisory' verdicts are
            only ever logged, never acted on by the graph.
        requires: Capabilities this plugin's role needs from model_router,
            same shape as an existing role's `requires:` in models.yaml.
        skill: Path to this plugin's SKILL.md, relative to its own
            directory. Defaults to "SKILL.md".
    """

    name: str
    role: str
    slot: Slot
    mode: Mode
    requires: list[Capability] = Field(default_factory=list)
    skill: str = "SKILL.md"

    @field_validator("name")
    @classmethod
    def _name_is_kebab_case(cls, value: str) -> str:
        if not _NAME_PATTERN.match(value):
            raise ValueError(
                f"name must be lowercase kebab-case (e.g. 'security-audit-reviewer'), got {value!r}"
            )
        return value

    @field_validator("role")
    @classmethod
    def _role_is_snake_case(cls, value: str) -> str:
        if not _ROLE_PATTERN.match(value):
            raise ValueError(
                f"role must be lowercase snake_case (e.g. 'security_audit_reviewer'), got {value!r}"
            )
        return value
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest services/orchestrator/plugins/tests/unit/test_manifest.py -v`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/plugins/__init__.py services/orchestrator/plugins/manifest.py services/orchestrator/plugins/tests
git commit -m "feat(orchestrator): add plugin manifest schema"
```

---

### Task 4: `services/orchestrator/plugins/discovery.py` — scan, validate, and load plugins

**Files:**
- Create: `services/orchestrator/plugins/discovery.py`
- Test: `services/orchestrator/plugins/tests/unit/test_discovery.py`
- Create: `plugins/.gitkeep` (the real top-level directory operators drop plugins into; empty by default)

**Interfaces:**
- Consumes: `PluginManifest` (Task 3), `skills.loader.load_skill_from_path` (Task 1).
- Produces: `LoadedPlugin` (dataclass: `manifest: PluginManifest`, `skill_body: str`), `discover_plugins(plugins_dir: pathlib.Path, known_roles: frozenset[str]) -> list[LoadedPlugin]`.

`known_roles` is every role name already declared in the loaded `models.yaml` (built-in roles) — a plugin whose `role` collides with one is rejected. Within one scan, a second plugin claiming an already-accepted plugin's `role` is also rejected. Every rejection (malformed YAML, schema validation failure, missing skill file, role collision) logs a warning naming the plugin directory and the reason, and is skipped — discovery never raises.

The manifest-parsing half needs `yaml` (lazy import per Global Constraints); the collision-detection half operates on already-parsed `PluginManifest` objects and needs no `yaml` at all, so it's tested separately with real, dependency-free pytest even in this sandbox.

- [ ] **Step 1: Write the failing tests**

```python
# services/orchestrator/plugins/tests/unit/test_discovery.py
"""Unit tests for plugin discovery."""

import logging
from pathlib import Path

import pytest

from services.orchestrator.plugins.discovery import (
    LoadedPlugin,
    _reject_role_collisions,
    discover_plugins,
)
from services.orchestrator.plugins.manifest import PluginManifest


def _manifest(name: str, role: str, slot: str = "after_reviewer") -> PluginManifest:
    return PluginManifest.model_validate(
        {"name": name, "role": role, "slot": slot, "mode": "gating", "requires": []}
    )


def test_reject_role_collisions_drops_plugin_matching_a_known_role(caplog):
    plugins = [LoadedPlugin(manifest=_manifest("x", "reviewer"), skill_body="body")]
    with caplog.at_level(logging.WARNING):
        survivors = _reject_role_collisions(plugins, known_roles=frozenset({"reviewer", "coder"}))
    assert survivors == []
    assert "reviewer" in caplog.text


def test_reject_role_collisions_drops_second_plugin_claiming_same_role(caplog):
    plugins = [
        LoadedPlugin(manifest=_manifest("first", "audit"), skill_body="a"),
        LoadedPlugin(manifest=_manifest("second", "audit"), skill_body="b"),
    ]
    with caplog.at_level(logging.WARNING):
        survivors = _reject_role_collisions(plugins, known_roles=frozenset())
    assert [p.manifest.name for p in survivors] == ["first"]


def test_reject_role_collisions_keeps_distinct_roles():
    plugins = [
        LoadedPlugin(manifest=_manifest("first", "audit"), skill_body="a"),
        LoadedPlugin(manifest=_manifest("second", "docs_audit"), skill_body="b"),
    ]
    survivors = _reject_role_collisions(plugins, known_roles=frozenset())
    assert len(survivors) == 2


def test_reject_role_collisions_drops_second_plugin_claiming_same_name(caplog):
    plugins = [
        LoadedPlugin(manifest=_manifest("dup-name", "audit_one"), skill_body="a"),
        LoadedPlugin(manifest=_manifest("dup-name", "audit_two"), skill_body="b"),
    ]
    with caplog.at_level(logging.WARNING):
        survivors = _reject_role_collisions(plugins, known_roles=frozenset())
    assert [p.manifest.role for p in survivors] == ["audit_one"]


def test_discover_plugins_on_missing_directory_returns_empty_list(tmp_path: Path):
    assert discover_plugins(tmp_path / "does-not-exist", known_roles=frozenset()) == []


def test_discover_plugins_loads_valid_plugin(tmp_path: Path):
    pytest.importorskip("yaml")
    plugin_dir = tmp_path / "security-audit-reviewer"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.yaml").write_text(
        "name: security-audit-reviewer\n"
        "role: security_audit_reviewer\n"
        "slot: after_reviewer\n"
        "mode: gating\n"
        "requires: [tool_calling]\n"
    )
    (plugin_dir / "SKILL.md").write_text(
        "---\nname: security-audit-reviewer\ndescription: Audits for security issues.\n---\n\nBody text.\n"
    )

    loaded = discover_plugins(tmp_path, known_roles=frozenset({"reviewer"}))

    assert len(loaded) == 1
    assert loaded[0].manifest.role == "security_audit_reviewer"
    assert loaded[0].skill_body == "Body text.\n"


def test_discover_plugins_skips_malformed_manifest_and_continues(tmp_path: Path, caplog):
    pytest.importorskip("yaml")
    bad_dir = tmp_path / "bad-plugin"
    bad_dir.mkdir()
    (bad_dir / "plugin.yaml").write_text("role: [not, a, string]\n")
    (bad_dir / "SKILL.md").write_text("---\nname: bad\ndescription: x\n---\n\nBody.\n")

    good_dir = tmp_path / "good-plugin"
    good_dir.mkdir()
    (good_dir / "plugin.yaml").write_text(
        "name: good-plugin\nrole: good_plugin\nslot: after_tester\nmode: advisory\nrequires: []\n"
    )
    (good_dir / "SKILL.md").write_text("---\nname: good-plugin\ndescription: x\n---\n\nBody.\n")

    with caplog.at_level(logging.WARNING):
        loaded = discover_plugins(tmp_path, known_roles=frozenset())

    assert [p.manifest.name for p in loaded] == ["good-plugin"]
    assert "bad-plugin" in caplog.text


def test_discover_plugins_skips_missing_skill_file(tmp_path: Path, caplog):
    pytest.importorskip("yaml")
    plugin_dir = tmp_path / "no-skill"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.yaml").write_text(
        "name: no-skill\nrole: no_skill\nslot: after_coder\nmode: advisory\nrequires: []\n"
    )

    with caplog.at_level(logging.WARNING):
        loaded = discover_plugins(tmp_path, known_roles=frozenset())

    assert loaded == []
    assert "no-skill" in caplog.text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest services/orchestrator/plugins/tests/unit/test_discovery.py -v`
Expected: `ModuleNotFoundError: No module named 'services.orchestrator.plugins.discovery'`

- [ ] **Step 3: Write the implementation**

```python
# services/orchestrator/plugins/discovery.py
"""Scans plugins/ at startup, validates each manifest, and loads its skill body.

Never raises: a malformed manifest, a missing skill file, or a role
collision skips that one plugin with a logged warning, exactly like
static_checks (ADR-046) skips a check whose tool/config is absent rather
than failing the whole node.
"""

import logging
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from services.orchestrator.plugins.manifest import PluginManifest
from skills.loader import load_skill_from_path

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LoadedPlugin:
    """A validated plugin manifest paired with its loaded skill body."""

    manifest: PluginManifest
    skill_body: str


def _load_one_plugin(plugin_dir: Path) -> LoadedPlugin | None:
    """Parses and validates one plugin directory's manifest + skill file.

    Returns:
        A LoadedPlugin, or None if anything about this plugin is invalid --
        the reason is always logged as a warning before returning None.
    """
    import yaml

    manifest_path = plugin_dir / "plugin.yaml"
    try:
        raw = yaml.safe_load(manifest_path.read_text())
        manifest = PluginManifest.model_validate(raw)
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        logger.warning("plugin '%s': invalid plugin.yaml, skipping: %s", plugin_dir.name, exc)
        return None

    skill_path = plugin_dir / manifest.skill
    try:
        skill = load_skill_from_path(skill_path)
    except (OSError, ValueError) as exc:
        logger.warning("plugin '%s': failed to load skill file, skipping: %s", plugin_dir.name, exc)
        return None

    return LoadedPlugin(manifest=manifest, skill_body=skill.body)


def _reject_role_collisions(
    plugins: list[LoadedPlugin], known_roles: frozenset[str]
) -> list[LoadedPlugin]:
    """Drops any plugin whose role or name collides with a built-in role or
    an already-accepted plugin in this same scan, in directory-scan order.

    Args:
        plugins: Plugins already individually validated by _load_one_plugin.
        known_roles: Role names already declared in the loaded models.yaml.

    Returns:
        The surviving plugins, in the same relative order.
    """
    survivors: list[LoadedPlugin] = []
    claimed_roles = set(known_roles)
    claimed_names: set[str] = set()
    for plugin in plugins:
        if plugin.manifest.role in claimed_roles:
            logger.warning(
                "plugin '%s': role '%s' collides with a built-in role or an "
                "already-accepted plugin, skipping",
                plugin.manifest.name, plugin.manifest.role,
            )
            continue
        if plugin.manifest.name in claimed_names:
            logger.warning(
                "plugin '%s': name collides with an already-accepted plugin, skipping",
                plugin.manifest.name,
            )
            continue
        claimed_roles.add(plugin.manifest.role)
        claimed_names.add(plugin.manifest.name)
        survivors.append(plugin)
    return survivors


def discover_plugins(plugins_dir: Path, known_roles: frozenset[str]) -> list[LoadedPlugin]:
    """Scans plugins_dir for plugin subdirectories and loads the valid ones.

    Args:
        plugins_dir: The plugins/ root directory. A missing directory is
            not an error -- a fresh install with no plugins yet installed
            returns an empty list.
        known_roles: Role names already declared in the loaded models.yaml,
            used to reject a plugin claiming a built-in role's name.

    Returns:
        Every plugin whose manifest validated, whose skill file loaded, and
        whose role doesn't collide with a built-in role or an
        earlier-accepted plugin -- in directory-scan (sorted) order.
    """
    if not plugins_dir.is_dir():
        return []

    loaded = [
        plugin
        for plugin_dir in sorted(plugins_dir.iterdir())
        if plugin_dir.is_dir() and (plugin_dir / "plugin.yaml").exists()
        for plugin in [_load_one_plugin(plugin_dir)]
        if plugin is not None
    ]
    return _reject_role_collisions(loaded, known_roles)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest services/orchestrator/plugins/tests/unit/test_discovery.py -v`
Expected: 8 passed if `pyyaml` is installed (5 pure-collision/missing-dir tests always pass here regardless: the 4 `_reject_role_collisions` tests plus `test_discover_plugins_on_missing_directory_returns_empty_list`); the `pytest.importorskip("yaml")`-guarded ones (`test_discover_plugins_loads_valid_plugin`, `test_discover_plugins_skips_malformed_manifest_and_continues`, `test_discover_plugins_skips_missing_skill_file`) SKIP cleanly in this sandbox instead of failing.

- [ ] **Step 5: Commit**

```bash
mkdir -p plugins && touch plugins/.gitkeep
git add services/orchestrator/plugins/discovery.py services/orchestrator/plugins/tests/unit/test_discovery.py plugins/.gitkeep
git commit -m "feat(orchestrator): discover and validate plugins/ manifests at startup"
```

---

### Task 5: Synthesize model_router role entries for discovered plugins

**Files:**
- Modify: `services/orchestrator/plugins/discovery.py` (add `synthesize_plugin_roles`)
- Test: `services/orchestrator/plugins/tests/unit/test_discovery.py` (add tests for the new function)

**Interfaces:**
- Consumes: `LoadedPlugin` (Task 4), `services.orchestrator.model_router.config.ModelsConfig`/`RoleConfig` (existing).
- Produces: `synthesize_plugin_roles(models_config: ModelsConfig, plugins: list[LoadedPlugin]) -> ModelsConfig`.

This function needs `services.orchestrator.model_router.config`, which imports `yaml` at module top level (a pre-existing characteristic of that file, unrelated to this plan) — so, per Global Constraints, this whole task is hand-traced in this sandbox rather than run for real, same tier as every other `model_router`-touching test in this codebase.

- [ ] **Step 1: Write the failing tests**

```python
# services/orchestrator/plugins/tests/unit/test_discovery.py -- append these tests
from services.orchestrator.model_router.config import AnthropicProviderConfig, ModelsConfig, RoleConfig
from services.orchestrator.plugins.discovery import synthesize_plugin_roles


def _base_models_config() -> ModelsConfig:
    return ModelsConfig(
        roles={"reviewer": RoleConfig(requires=["tool_calling"], prefer=["claude_sonnet"])},
        providers={
            "claude_sonnet": AnthropicProviderConfig(
                type="anthropic", model="claude-sonnet-5",
                capabilities=["tool_calling"],
                cost_per_1m_input_tokens=3.0, cost_per_1m_output_tokens=15.0,
            )
        },
    )


def test_synthesize_plugin_roles_adds_new_role_with_default_prefer():
    config = _base_models_config()
    plugin = LoadedPlugin(manifest=_manifest("audit", "security_audit_reviewer"), skill_body="x")

    result = synthesize_plugin_roles(config, [plugin])

    assert result.roles["security_audit_reviewer"].prefer == ["local", "claude_sonnet"]
    assert result.roles["security_audit_reviewer"].requires == []
    assert "reviewer" in result.roles  # existing roles untouched


def test_synthesize_plugin_roles_does_not_override_an_explicit_entry():
    config = _base_models_config()
    config.roles["security_audit_reviewer"] = RoleConfig(requires=[], prefer=["claude_opus"])
    plugin = LoadedPlugin(manifest=_manifest("audit", "security_audit_reviewer"), skill_body="x")

    result = synthesize_plugin_roles(config, [plugin])

    assert result.roles["security_audit_reviewer"].prefer == ["claude_opus"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest services/orchestrator/plugins/tests/unit/test_discovery.py -v`
Expected (dependencies installed): `ImportError: cannot import name 'synthesize_plugin_roles'`. In this sandbox: hand-trace (module import already blocked by the pre-existing `yaml` top-level import in `model_router/config.py`).

- [ ] **Step 3: Write the implementation**

```python
# services/orchestrator/plugins/discovery.py -- add
from services.orchestrator.model_router.config import ModelsConfig, RoleConfig

DEFAULT_PLUGIN_PREFER = ["local", "claude_sonnet"]
"""Matches the built-in 'reviewer' role's own default prefer chain (a
plugin role's job -- render a verdict on a diff -- is the same shape)."""


def synthesize_plugin_roles(
    models_config: ModelsConfig, plugins: list[LoadedPlugin]
) -> ModelsConfig:
    """Adds a model_router role entry per plugin lacking an explicit one.

    Args:
        models_config: The loaded, validated models.yaml contents.
        plugins: Plugins that survived discovery (Task 4).

    Returns:
        A new ModelsConfig with one additional roles entry per plugin whose
        role name isn't already declared in models_config.roles -- an
        operator-declared entry always wins over a synthesized one.
    """
    new_roles = dict(models_config.roles)
    for plugin in plugins:
        if plugin.manifest.role in new_roles:
            continue
        new_roles[plugin.manifest.role] = RoleConfig(
            requires=plugin.manifest.requires, prefer=list(DEFAULT_PLUGIN_PREFER)
        )
    return models_config.model_copy(update={"roles": new_roles})
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest services/orchestrator/plugins/tests/unit/test_discovery.py -v`
Expected (dependencies installed): all pass. In this sandbox: hand-trace against `RoleConfig`'s definition (`requires: list[Capability]`, `prefer: list[str]` with a non-empty validator) confirmed in `model_router/config.py` -- `plugin.manifest.requires` is already `list[Capability]` (Task 3's schema), so no conversion is needed, and `["local", "claude_sonnet"]` is non-empty so the `_prefer_not_empty` validator passes.

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/plugins/discovery.py services/orchestrator/plugins/tests/unit/test_discovery.py
git commit -m "feat(orchestrator): synthesize model_router roles for discovered plugins"
```

---

### Task 6: Extract `_reopen_last_done_task_or_block` from `node_static_checks`

**Files:**
- Modify: `services/orchestrator/graph/nodes.py`
- Modify: `services/orchestrator/agents/tests/unit/test_static_checks.py` (if it asserts on `node_static_checks`'s exact reopen-logic behavior, no change needed beyond confirming it still passes; add one direct test of the extracted helper)

**Interfaces:**
- Produces: `_reopen_last_done_task_or_block(state: ProjectState, feedback: AIMessage, curator_source: str) -> dict[str, Any]`, reused unchanged by `node_static_checks` and (Task 7) `node_plugin`.

Pure refactor, no behavior change for `node_static_checks` — this task's test proves that. `graph/nodes.py` imports `langchain_core`, so this task is hand-traced in this sandbox.

- [ ] **Step 1: Write the failing test**

```python
# services/orchestrator/graph/tests/unit/test_nodes.py -- add this test
# (find this file's existing static_checks fixtures -- a task_queue with one
# "done" task at MAX_TASK_ATTEMPTS - 1 attempts -- and reuse the same shape)
from langchain_core.messages import AIMessage

from services.orchestrator.graph.nodes import _reopen_last_done_task_or_block


def test_reopen_last_done_task_or_block_reopens_the_last_done_task():
    state = _make_state(  # this file's existing state-building helper
        task_queue=[{"description": "t1", "status": "done", "attempts": 0}]
    )
    update = _reopen_last_done_task_or_block(state, AIMessage(content="feedback"), "static_checks")
    assert update["phase"] == "implementing"
    assert update["task_queue"][0]["status"] == "in_progress"
    assert update["task_queue"][0]["attempts"] == 1


def test_reopen_last_done_task_or_block_blocks_at_max_attempts():
    state = _make_state(
        task_queue=[{"description": "t1", "status": "done", "attempts": MAX_TASK_ATTEMPTS - 1}]
    )
    update = _reopen_last_done_task_or_block(state, AIMessage(content="feedback"), "static_checks")
    assert update["phase"] == "blocked"
    assert update["task_queue"][0]["status"] == "blocked"


def test_reopen_last_done_task_or_block_blocks_when_no_done_task_exists():
    state = _make_state(task_queue=[])
    update = _reopen_last_done_task_or_block(state, AIMessage(content="feedback"), "static_checks")
    assert update["phase"] == "blocked"
    assert "task_queue" not in update
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest services/orchestrator/graph/tests/unit/test_nodes.py -k reopen_last_done_task -v`
Expected (dependencies installed): `ImportError: cannot import name '_reopen_last_done_task_or_block'`. In this sandbox: hand-trace (blocked by `langchain_core`/`langgraph`).

- [ ] **Step 3: Write the implementation**

In `graph/nodes.py`, add the extracted helper just above `node_static_checks`:

```python
def _reopen_last_done_task_or_block(
    state: ProjectState, feedback: AIMessage, curator_source: str
) -> dict[str, Any]:
    """Reopens the most recently completed task for another attempt.

    Shared by node_static_checks and node_plugin (a 'gating' plugin's
    changes-requested verdict, graph/plugin_node.py) -- both feed a gating
    finding back to the Coder the same way: reopen the last-done task,
    honoring MAX_TASK_ATTEMPTS, or block the project if there's no
    completed task to reopen at all.

    Args:
        state: The current project state.
        feedback: The failure/finding message attached to `messages`.
        curator_source: Recorded as `curator_source` on a blocked outcome,
            so the Memory Curator's summary can say what blocked it.

    Returns:
        A state update: task_queue/phase="implementing" on a normal
        reopen; task_queue/phase="blocked" at MAX_TASK_ATTEMPTS; just
        phase="blocked" (no task_queue key) when there's no done task at all.
    """
    task_queue = state["task_queue"]
    last_done_index = next(
        (i for i in range(len(task_queue) - 1, -1, -1) if task_queue[i]["status"] == "done"),
        None,
    )
    if last_done_index is None:
        return {"phase": "blocked", "curator_source": curator_source, "messages": [feedback]}

    reopened = {
        **task_queue[last_done_index],
        "status": "in_progress",
        "attempts": task_queue[last_done_index]["attempts"] + 1,
    }
    if reopened["attempts"] >= MAX_TASK_ATTEMPTS:
        reopened["status"] = "blocked"
        return {
            "task_queue": _replace_task(task_queue, last_done_index, reopened),
            "phase": "blocked",
            "curator_source": curator_source,
            "messages": [feedback],
        }
    return {
        "task_queue": _replace_task(task_queue, last_done_index, reopened),
        "phase": "implementing",
        "messages": [feedback],
    }
```

Then replace `node_static_checks`'s failure branch (everything from `task_queue = state["task_queue"]` through its final `return` in the failure path) with:

```python
    return _reopen_last_done_task_or_block(state, feedback, "static_checks")
```

removing the now-duplicated `last_done_index`/`reopened`/attempts logic and its two log statements from `node_static_checks` itself (the shared helper has no logging of its own — add a single `logger.info`/`logger.warning` call in `node_static_checks` right before the `return`, matching what it logged before, so the log line's caller-specific message ("static checks failed...") is preserved even though the reopen mechanics moved).

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest services/orchestrator/graph/tests/unit/test_nodes.py -v`
Expected (dependencies installed): all pass, including `node_static_checks`'s pre-existing tests (unchanged behavior) and the 3 new direct tests of the helper. In this sandbox: hand-trace line-by-line that the extracted helper is byte-for-byte the same logic `node_static_checks` had inline, just parameterized by `feedback`/`curator_source` instead of the check-output-specific `AIMessage` and the literal string `"static_checks"`.

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/graph/nodes.py services/orchestrator/graph/tests/unit/test_nodes.py
git commit -m "refactor(orchestrator): extract _reopen_last_done_task_or_block from node_static_checks"
```

---

### Task 7: `services/orchestrator/graph/plugin_node.py` — the generic plugin node

**Files:**
- Create: `services/orchestrator/graph/plugin_node.py`
- Test: `services/orchestrator/graph/tests/unit/test_plugin_node.py`

**Interfaces:**
- Consumes: `PluginManifest`/`LoadedPlugin` (Tasks 3-4), `_reopen_last_done_task_or_block`/`_with_cost` (`graph/nodes.py`, Task 6), `invoke_structured` (`agents/llm.py`, existing), `ModelRouter`/`ExecutorClient` (existing).
- Produces: `PluginFinding`, `PluginVerdict` (Pydantic), `run_plugin_review(router, executor_client, project_id, role, skill_body, changed_files) -> tuple[PluginVerdict, AIMessage]`, `node_plugin(state, router, executor_client, manifest, skill_body, next_node) -> dict[str, Any]`, `route_after_plugin(state, next_node) -> str`.

`node_plugin`'s contract: 'gating' mode with a `changes-requested` verdict routes through `_reopen_last_done_task_or_block` exactly like `node_static_checks`; 'advisory' mode (or a 'gating' plugin's `approve` verdict) always returns `{}` (cost update only) and lets `route_after_plugin` send control to `next_node` — the next plugin in this slot's chain, or the slot's real exit node, both decided at graph-build time (Task 8).

Imports `langchain_core`/depends on `ModelRouter` — hand-traced in this sandbox, same as Task 6.

- [ ] **Step 1: Write the failing tests**

```python
# services/orchestrator/graph/tests/unit/test_plugin_node.py
"""Unit tests for the generic plugin graph node."""

import asyncio

from services.orchestrator.agents.tests.unit.router_fakes import FakeRouter, structured
from services.orchestrator.graph.plugin_node import (
    PluginFinding,
    PluginVerdict,
    node_plugin,
    route_after_plugin,
    run_plugin_review,
)
from services.orchestrator.plugins.manifest import PluginManifest

APPROVE = PluginVerdict(verdict="approve", findings=[])
CHANGES_REQUESTED = PluginVerdict(
    verdict="changes-requested",
    findings=[PluginFinding(file="a.py", line=1, severity="critical", message="bad")],
)


class RecordingExecutorClient:
    def __init__(self, files: dict[str, str] | None = None):
        self.files = dict(files or {})

    async def read_file(self, project_id: str, path: str) -> str:
        return self.files[path]


def _gating_manifest() -> PluginManifest:
    return PluginManifest.model_validate(
        {
            "name": "security-audit-reviewer", "role": "security_audit_reviewer",
            "slot": "after_reviewer", "mode": "gating", "requires": ["tool_calling"],
        }
    )


def _advisory_manifest() -> PluginManifest:
    return PluginManifest.model_validate(
        {
            "name": "style-advisor", "role": "style_advisor",
            "slot": "after_coder", "mode": "advisory", "requires": [],
        }
    )


def test_run_plugin_review_reads_changed_files_and_returns_verdict():
    router = FakeRouter([structured(APPROVE)])
    executor = RecordingExecutorClient(files={"a.py": "print(1)\n"})

    result, message = asyncio.run(
        run_plugin_review(router, executor, "proj-1", "security_audit_reviewer", "Skill body.", ["a.py"])
    )

    assert result.verdict == "approve"
    assert message.usage_metadata is not None
    assert "Skill body." in router.model.requests[0][0].content


def test_node_plugin_gating_changes_requested_reopens_task_and_routes_to_coder():
    router = FakeRouter([structured(CHANGES_REQUESTED)])
    executor = RecordingExecutorClient(files={"a.py": "x"})
    state = _make_state(  # this test module's own state-building helper
        task_queue=[{"description": "t1", "status": "done", "attempts": 0}],
        changed_files=["a.py"],
    )

    update = node_plugin(
        state, router=router, executor_client=executor,
        manifest=_gating_manifest(), skill_body="Skill body.", next_node="frontend_review",
    )

    assert update["phase"] == "implementing"
    assert update["task_queue"][0]["attempts"] == 1
    assert route_after_plugin({**state, **update}, next_node="frontend_review") == "coder"


def test_node_plugin_gating_approve_continues_to_next_node():
    router = FakeRouter([structured(APPROVE)])
    executor = RecordingExecutorClient(files={"a.py": "x"})
    state = _make_state(task_queue=[], changed_files=["a.py"])

    update = node_plugin(
        state, router=router, executor_client=executor,
        manifest=_gating_manifest(), skill_body="Skill body.", next_node="frontend_review",
    )

    assert update == {"cost_usd_session": state["cost_usd_session"], "cost_usd_weekly": state["cost_usd_weekly"]}
    assert route_after_plugin({**state, **update}, next_node="frontend_review") == "frontend_review"


def test_node_plugin_advisory_changes_requested_never_reopens_a_task():
    router = FakeRouter([structured(CHANGES_REQUESTED)])
    executor = RecordingExecutorClient(files={"a.py": "x"})
    state = _make_state(
        task_queue=[{"description": "t1", "status": "done", "attempts": 0}],
        changed_files=["a.py"],
    )

    update = node_plugin(
        state, router=router, executor_client=executor,
        manifest=_advisory_manifest(), skill_body="Skill body.", next_node="reviewer",
    )

    assert "task_queue" not in update
    assert route_after_plugin({**state, **update}, next_node="reviewer") == "reviewer"
```

(`_make_state` follows this test module's own convention for building a minimal `ProjectState`; match whatever helper `test_nodes.py` already uses for the same purpose, including `cost_usd_session`/`cost_usd_weekly` starting at `0.0`.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest services/orchestrator/graph/tests/unit/test_plugin_node.py -v`
Expected (dependencies installed): `ModuleNotFoundError: No module named 'services.orchestrator.graph.plugin_node'`. In this sandbox: hand-trace.

- [ ] **Step 3: Write the implementation**

```python
# services/orchestrator/graph/plugin_node.py
"""Generic LangGraph node for a plugin-declared agent role (2026-08-06 design).

Every plugin role executes through this one node, parameterized by its
manifest -- structurally identical to node_reviewer/node_static_checks's
verdict-on-a-diff shape, just with the skill text and gating/advisory
behavior supplied by the plugin instead of hardcoded.
"""

import asyncio
import logging
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from services.orchestrator.agents.llm import invoke_structured
from services.orchestrator.executor_client import ExecutorClient
from services.orchestrator.graph.nodes import _reopen_last_done_task_or_block, _with_cost
from services.orchestrator.graph.state import ProjectState
from services.orchestrator.model_router.router import ModelRouter
from services.orchestrator.plugins.manifest import PluginManifest

logger = logging.getLogger(__name__)


class PluginFinding(BaseModel):
    """A single plugin-review finding with file, line, severity, and message."""

    file: str = Field(description="File path where the issue was found")
    line: int = Field(description="Approximate line number of the issue")
    severity: str = Field(description="'critical' or 'warning'")
    message: str = Field(description="Concise description of the issue")


class PluginVerdict(BaseModel):
    """A plugin role's verdict and structured findings."""

    verdict: str = Field(
        description="'approve' if no issues; 'changes-requested' if at least one finding"
    )
    findings: list[PluginFinding] = Field(default_factory=list)


def _build_user_prompt(project_id: str, files: dict[str, str]) -> str:
    parts = [f"Project: {project_id}\n\n"]
    for path, content in sorted(files.items()):
        parts.append(f"### {path}\n```\n{content}\n```\n")
    return "\n".join(parts)


async def run_plugin_review(
    router: ModelRouter,
    executor_client: ExecutorClient,
    project_id: str,
    role: str,
    skill_body: str,
    changed_files: list[str],
) -> tuple[PluginVerdict, AIMessage]:
    """Runs one plugin role's review pass over the project's changed files.

    Args:
        router: The ModelRouter used to resolve `role`'s provider.
        executor_client: The Executor client used to read file contents.
        project_id: The project identifier.
        role: The plugin's model_router role name.
        skill_body: The plugin's SKILL.md body, sent as the system prompt.
        changed_files: File paths modified during coding.

    Returns:
        A tuple of (PluginVerdict, raw AIMessage) -- the raw message carries
        usage_metadata for the calling node to price via ModelRouter.cost_usd().
    """
    llm = router.resolve(role)
    files: dict[str, str] = {}
    for path in changed_files:
        try:
            files[path] = await executor_client.read_file(project_id, path)
        except Exception as exc:
            logger.warning(
                "project %s: plugin role '%s' failed to read '%s': %s",
                project_id, role, path, exc,
            )

    messages = [
        SystemMessage(content=skill_body),
        HumanMessage(content=_build_user_prompt(project_id, files)),
    ]
    return await invoke_structured(llm, PluginVerdict, messages)


def node_plugin(
    state: ProjectState,
    router: ModelRouter,
    executor_client: ExecutorClient,
    manifest: PluginManifest,
    skill_body: str,
    next_node: str,
) -> dict[str, Any]:
    """Runs one plugin role's review pass and updates state accordingly.

    A 'gating' plugin's changes-requested verdict reopens the most recently
    completed task for another Coder attempt (MAX_TASK_ATTEMPTS-bounded,
    same contract node_static_checks uses). Every other outcome --
    'gating' approve, or any 'advisory' verdict -- is a cost-only update;
    route_after_plugin sends control to `next_node`.

    Args:
        state: The current project state.
        router: The ModelRouter this plugin's role resolves through.
        executor_client: The Executor client used to read changed files.
        manifest: This plugin's validated manifest.
        skill_body: This plugin's SKILL.md body.
        next_node: This slot's next node -- the next plugin in the chain,
            or the slot's real exit node if this is the last one (baked in
            at graph-build time, graph/build.py).

    Returns:
        A state update: task_queue/phase/messages on a gating
        changes-requested outcome (or a blocked transition at
        MAX_TASK_ATTEMPTS); a cost-only update otherwise.
    """
    result, ai_message = asyncio.run(
        run_plugin_review(
            router, executor_client, state["project_id"], manifest.role, skill_body,
            state["changed_files"],
        )
    )
    cost = router.cost_usd(manifest.role, ai_message.usage_metadata)

    if manifest.mode == "gating" and result.verdict == "changes-requested":
        findings_text = "\n".join(
            f"- {f.file}:{f.line} [{f.severity}] {f.message}" for f in result.findings
        )
        feedback = AIMessage(content=f"{manifest.name} feedback:\n{findings_text}")
        update = _reopen_last_done_task_or_block(state, feedback, manifest.name)
        return _with_cost(state, update, cost)

    if result.verdict == "changes-requested":
        logger.info(
            "project %s: advisory plugin '%s' flagged %d finding(s), continuing",
            state["project_id"], manifest.name, len(result.findings),
        )

    return _with_cost(state, {}, cost)


def route_after_plugin(state: ProjectState, next_node: str) -> str:
    """Routes to Coder on a gating changes-requested, the Memory Curator on
    blocked, or onward to `next_node` otherwise."""
    if state["phase"] == "blocked":
        return "memory_curator"
    if state["phase"] == "implementing":
        return "coder"
    return next_node
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest services/orchestrator/graph/tests/unit/test_plugin_node.py -v`
Expected (dependencies installed): all pass. In this sandbox: hand-trace against Task 6's `_reopen_last_done_task_or_block` and `node_reviewer`'s existing `_with_cost` usage pattern, both already read in full during planning.

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/graph/plugin_node.py services/orchestrator/graph/tests/unit/test_plugin_node.py
git commit -m "feat(orchestrator): add generic plugin graph node"
```

---

### Task 8: Wire plugins into `graph/build.py` and `main.py`

**Files:**
- Modify: `services/orchestrator/graph/build.py` (`build_project_graph` gains a `plugins: list[LoadedPlugin]` parameter; add `_wire_plugin_slot` and `_plugin_node_name`; remap the `static_checks`→reviewer, `reviewer`→frontend_review, and `testing`→memory_curator edges through it)
- Modify: `services/orchestrator/service.py` (`OrchestratorService.__init__` threads `plugins` through to `build_project_graph`, mirroring how it already threads `stacks_config`)
- Modify: `services/orchestrator/main.py` (`main()` calls `discover_plugins`/`synthesize_plugin_roles` before building the router, and passes the filtered plugin list to `OrchestratorService`)
- Test: `services/orchestrator/graph/tests/unit/test_build.py` (create if it doesn't already exist, or extend it)

**Interfaces:**
- Consumes: everything from Tasks 3-7.
- Produces: `build_project_graph(..., plugins: list[LoadedPlugin])`; no other public signature changes.

Hand-traced in this sandbox (`langgraph` dependency).

- [ ] **Step 1: Write the failing test**

```python
# services/orchestrator/graph/tests/unit/test_build.py -- add this test
# (reuses this file's/graph/tests/unit's existing checkpointer/router/executor_client
# fixtures -- match whatever build_project_graph's other tests already construct)
from services.orchestrator.plugins.discovery import LoadedPlugin
from services.orchestrator.plugins.manifest import PluginManifest


def test_build_project_graph_with_no_plugins_wires_exactly_as_before():
    graph = build_project_graph(
        checkpointer(), router(), executor_client(), memory_conn(), projects_root(),
        stacks_config(), plugins=[],
    )
    node_names = set(graph.get_graph().nodes)
    assert "reviewer" in node_names
    assert not any(name.startswith("plugin_") for name in node_names)


def test_build_project_graph_wires_an_after_reviewer_plugin_between_reviewer_and_frontend_review():
    manifest = PluginManifest.model_validate(
        {
            "name": "security-audit-reviewer", "role": "security_audit_reviewer",
            "slot": "after_reviewer", "mode": "gating", "requires": ["tool_calling"],
        }
    )
    plugin = LoadedPlugin(manifest=manifest, skill_body="Audit for security issues.")

    graph = build_project_graph(
        checkpointer(), router(), executor_client(), memory_conn(), projects_root(),
        stacks_config(), plugins=[plugin],
    )

    node_names = set(graph.get_graph().nodes)
    assert "plugin_security_audit_reviewer" in node_names
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest services/orchestrator/graph/tests/unit/test_build.py -v`
Expected (dependencies installed): `TypeError: build_project_graph() got an unexpected keyword argument 'plugins'`. In this sandbox: hand-trace.

- [ ] **Step 3: Write the implementation**

`graph/build.py`:

```python
import functools

from services.orchestrator.graph.plugin_node import node_plugin, route_after_plugin
from services.orchestrator.plugins.discovery import LoadedPlugin


def _plugin_node_name(manifest) -> str:
    return f"plugin_{manifest.role}"


def _wire_plugin_slot(
    builder: StateGraph,
    plugins: list[LoadedPlugin],
    router: ModelRouter,
    executor_client: ExecutorClient,
    exit_node: str,
) -> str:
    """Adds one node per plugin, chained in order and ending at exit_node.

    Args:
        builder: The graph builder being constructed.
        plugins: This slot's plugins, in the order they should run.
        router: Threaded through to every plugin node.
        executor_client: Threaded through to every plugin node.
        exit_node: The slot's real destination once every plugin in the
            chain has passed (or if the chain is empty).

    Returns:
        The entry node name the slot's fixed predecessor edge should route
        to -- exit_node itself if plugins is empty, degrading to a pure
        passthrough exactly like static_checks with nothing to check.
    """
    if not plugins:
        return exit_node

    chain_targets = [_plugin_node_name(p.manifest) for p in plugins] + [exit_node]
    for position, plugin in enumerate(plugins):
        node_name = chain_targets[position]
        next_node = chain_targets[position + 1]
        builder.add_node(
            node_name,
            functools.partial(
                node_plugin,
                router=router,
                executor_client=executor_client,
                manifest=plugin.manifest,
                skill_body=plugin.skill_body,
                next_node=next_node,
            ),
        )
        builder.add_conditional_edges(
            node_name,
            functools.partial(route_after_plugin, next_node=next_node),
            {"coder": "coder", "memory_curator": "memory_curator", next_node: next_node},
        )
    return chain_targets[0]
```

In `build_project_graph`'s signature, add `plugins: list[LoadedPlugin]` as a new required parameter (after `stacks_config`, before `global_cost_threshold_usd`) and update its docstring's `Args` section to describe it (mirrors `stacks_config`'s own doc style: "Discovered plugins (services/orchestrator/plugins/discovery.py), already validated and role-collision-filtered -- an empty list wires the graph exactly as before this feature existed").

After the existing `builder.add_node(...)` calls (before the `add_edge`/`add_conditional_edges` block), insert:

```python
    plugins_by_slot: dict[str, list[LoadedPlugin]] = {"after_coder": [], "after_reviewer": [], "after_tester": []}
    for plugin in plugins:
        plugins_by_slot[plugin.manifest.slot].append(plugin)

    after_coder_entry = _wire_plugin_slot(builder, plugins_by_slot["after_coder"], router, executor_client, "reviewer")
    after_reviewer_entry = _wire_plugin_slot(builder, plugins_by_slot["after_reviewer"], router, executor_client, "frontend_review")
    after_tester_entry = _wire_plugin_slot(builder, plugins_by_slot["after_tester"], router, executor_client, "memory_curator")
```

Then change exactly three existing lines:

```python
    # was: {"coder": "coder", "reviewer": "reviewer", "memory_curator": "memory_curator"}
    builder.add_conditional_edges(
        "static_checks",
        route_after_static_checks,
        {"coder": "coder", "reviewer": after_coder_entry, "memory_curator": "memory_curator"},
    )
    # was: {"coder": "coder", "frontend_review": "frontend_review", "memory_curator": "memory_curator"}
    builder.add_conditional_edges(
        "reviewer",
        route_after_reviewer,
        {"coder": "coder", "frontend_review": after_reviewer_entry, "memory_curator": "memory_curator"},
    )
    # was: builder.add_edge("testing", "memory_curator")
    builder.add_edge("testing", after_tester_entry)
```

No other line in `build_project_graph` changes. `route_after_static_checks`/`route_after_reviewer` themselves are untouched (Design decision from the brainstorm: the dict *values* passed to `add_conditional_edges` change, not the routing functions' own logic).

`service.py`: add a `plugins: list[LoadedPlugin]` parameter to `OrchestratorService.__init__` (same position/style as `stacks_config`) and pass it straight through to the one `build_project_graph(...)` call site.

`main.py`:

```python
from services.orchestrator.plugins.discovery import discover_plugins, synthesize_plugin_roles
from services.orchestrator.model_router.errors import NoCapableProviderError

DEFAULT_PLUGINS_DIR = "plugins"


def _build_router_and_plugins() -> tuple[ModelRouter, list]:
    models_config_path = Path(os.environ.get("MODELS_CONFIG_PATH", DEFAULT_MODELS_CONFIG_PATH))
    models_config = load_models_config(models_config_path)
    plugins_dir = Path(os.environ.get("PLUGINS_DIR", DEFAULT_PLUGINS_DIR))
    discovered = discover_plugins(plugins_dir, known_roles=frozenset(models_config.roles))
    models_config = synthesize_plugin_roles(models_config, discovered)
    anthropic_api_key = (DEFAULT_SECRETS_DIR / "claude_api_key").read_text().strip()
    router = ModelRouter(models_config, anthropic_api_key=anthropic_api_key)

    resolvable_plugins = []
    for plugin in discovered:
        try:
            router.resolve(plugin.manifest.role)
        except NoCapableProviderError as exc:
            logging.getLogger(__name__).warning(
                "plugin '%s': no configured provider satisfies its required "
                "capabilities, skipping: %s", plugin.manifest.name, exc,
            )
            continue
        resolvable_plugins.append(plugin)
    return router, resolvable_plugins
```

Replace the old `_build_router()` function and its one call site in `main()` with `_build_router_and_plugins()`, threading the returned `plugins` list into the `OrchestratorService(...)` construction call alongside the existing `stacks_config` argument.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest services/orchestrator/graph/tests/unit/test_build.py -v`
Expected (dependencies installed): all pass, plus every pre-existing `graph/tests/unit` test still passes with `plugins=[]` added to each of their `build_project_graph(...)` call sites (grep every existing call site in that test directory and add the new argument — none of their assertions should need to change, since an empty plugin list degrades to the exact prior topology). In this sandbox: hand-trace the three edge-remapping changes against `build.py`'s full pre-change text (read in full during planning) to confirm no other line depends on `"reviewer"`/`"frontend_review"`/`"memory_curator"` being literal, hardcoded destinations elsewhere in the file.

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/graph/build.py services/orchestrator/service.py services/orchestrator/main.py services/orchestrator/graph/tests/unit/test_build.py
git commit -m "feat(orchestrator): wire discovered plugins into the graph at their declared slots"
```

---

### Task 9: End-to-end fixture proof

**Files:**
- Create: `services/orchestrator/plugins/tests/fixtures/example-plugin/plugin.yaml`, `.../SKILL.md` (a test-only fixture, not installed under the real top-level `plugins/`)
- Test: `services/orchestrator/graph/tests/unit/test_build.py` (one more test using the on-disk fixture through the real `discover_plugins` → `build_project_graph` path)

**Interfaces:** none new — this task only proves Tasks 3-8 compose correctly end to end, the way every prior phase's plan ends with one integration-shaped proof.

- [ ] **Step 1: Write the failing test**

```python
# services/orchestrator/graph/tests/unit/test_build.py -- add this test
from pathlib import Path

from services.orchestrator.plugins.discovery import discover_plugins

FIXTURE_PLUGINS_DIR = Path(__file__).parents[3] / "plugins" / "tests" / "fixtures"


def test_discovered_fixture_plugin_wires_into_a_real_graph_build():
    import pytest
    pytest.importorskip("yaml")

    plugins = discover_plugins(FIXTURE_PLUGINS_DIR, known_roles=frozenset({"reviewer", "coder"}))
    assert [p.manifest.name for p in plugins] == ["example-plugin"]

    graph = build_project_graph(
        checkpointer(), router(), executor_client(), memory_conn(), projects_root(),
        stacks_config(), plugins=plugins,
    )
    assert "plugin_example_reviewer" in set(graph.get_graph().nodes)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest services/orchestrator/graph/tests/unit/test_build.py -k discovered_fixture -v`
Expected: fails on `assert [p.manifest.name for p in plugins] == ["example-plugin"]` — the fixture directory doesn't exist yet. In this sandbox: hand-trace.

- [ ] **Step 3: Create the fixture**

```yaml
# services/orchestrator/plugins/tests/fixtures/example-plugin/plugin.yaml
name: example-plugin
role: example_reviewer
slot: after_reviewer
mode: advisory
requires: [tool_calling]
```

```markdown
# services/orchestrator/plugins/tests/fixtures/example-plugin/SKILL.md
---
name: example-plugin
description: A fixture plugin used only by graph/tests/unit/test_build.py to prove plugin discovery and graph wiring compose end to end.
---

# Example Plugin

This is a test fixture, not a real reviewer skill. Approve everything.
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest services/orchestrator/graph/tests/unit/test_build.py -k discovered_fixture -v`
Expected (dependencies installed): passes. In this sandbox: passes through `discover_plugins` (Task 4, real pytest already proven) if `pyyaml` happens to be installed, otherwise skips via the `importorskip` guard; hand-trace the `build_project_graph` half.

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/plugins/tests/fixtures services/orchestrator/graph/tests/unit/test_build.py
git commit -m "test(orchestrator): prove plugin discovery and graph wiring compose end to end"
```

---

## Definition of Done

- `skills/loader.py` is real, tested, and every one of the four built-in LLM-backed agent modules (`planner.py`'s critique step, `coder.py`, `reviewer.py`, `frontend_reviewer.py`) loads its prompt from the matching `skills/*/SKILL.md` instead of a hardcoded string (closes ADR-014).
- A plugin dropped into `plugins/<name>/` with a valid `plugin.yaml` + `SKILL.md` is auto-discovered at Orchestrator startup, gets a synthesized `model_router` role if `models.yaml` doesn't already declare one, and its node is wired into the graph at its declared slot (`after_coder`/`after_reviewer`/`after_tester`) — proven by Task 9's fixture.
- An invalid plugin (bad manifest, missing skill file, capability the deployment can't satisfy, role collision) is skipped with a logged warning and never crashes Orchestrator startup or silently joins the graph.
- `plugins=[]` (no plugins installed) produces a graph topology byte-for-byte identical to before this feature existed — proven by Task 8's first new test.
- Every task's real-pytest-runnable steps pass in an environment with this repo's full dependencies installed; every hand-traced step is explicitly marked as such per this plan's Global Constraints, matching this repo's existing phase-verification convention.
