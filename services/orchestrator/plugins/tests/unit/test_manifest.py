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
                "name": "x",
                "role": "x",
                "slot": "before_planner",
                "mode": "gating",
                "requires": [],
            }
        )


def test_manifest_rejects_unknown_mode():
    with pytest.raises(ValidationError):
        PluginManifest.model_validate(
            {
                "name": "x",
                "role": "x",
                "slot": "after_coder",
                "mode": "silent",
                "requires": [],
            }
        )


def test_manifest_rejects_non_snake_case_role():
    with pytest.raises(ValidationError, match="role"):
        PluginManifest.model_validate(
            {
                "name": "x",
                "role": "Not-Snake-Case",
                "slot": "after_coder",
                "mode": "gating",
                "requires": [],
            }
        )


def test_manifest_rejects_uppercase_name():
    with pytest.raises(ValidationError, match="name"):
        PluginManifest.model_validate(
            {
                "name": "NotKebabCase",
                "role": "x",
                "slot": "after_coder",
                "mode": "gating",
                "requires": [],
            }
        )


def test_manifest_accepts_explicit_skill_path():
    manifest = PluginManifest.model_validate(
        {
            "name": "x",
            "role": "x",
            "slot": "after_tester",
            "mode": "advisory",
            "requires": [],
            "skill": "skill-body.md",
        }
    )
    assert manifest.skill == "skill-body.md"


def test_manifest_rejects_skill_path_with_traversal():
    with pytest.raises(ValidationError, match="skill"):
        PluginManifest.model_validate(
            {
                "name": "x",
                "role": "x",
                "slot": "after_tester",
                "mode": "advisory",
                "requires": [],
                "skill": "../../../etc/passwd",
            }
        )


def test_manifest_rejects_absolute_skill_path():
    with pytest.raises(ValidationError, match="skill"):
        PluginManifest.model_validate(
            {
                "name": "x",
                "role": "x",
                "slot": "after_tester",
                "mode": "advisory",
                "requires": [],
                "skill": "/etc/passwd",
            }
        )
