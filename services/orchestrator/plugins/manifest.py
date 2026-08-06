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
