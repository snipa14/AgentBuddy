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
