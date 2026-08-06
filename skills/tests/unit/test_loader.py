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
