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
