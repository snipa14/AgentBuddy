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
