"""Unit tests for Telegram /new name/slug derivation and dedupe helpers."""

import pytest

from services.orchestrator.registry.naming import (
    dedupe_id,
    dedupe_name,
    derive_project_name,
    slugify,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("/new Build me a CLI that converts CSV to JSON", "Build Me A Cli That Converts"),
        ("Build me a CLI", "Build Me A Cli"),
        ("/new", "Untitled Project"),
        ("   ", "Untitled Project"),
        ("/new !!! ???", "Untitled Project"),
        ("/new hello, world! it's great", "Hello World It S Great"),
    ],
)
def test_derive_project_name_from_message_text(text, expected):
    assert derive_project_name(text) == expected


def test_derive_project_name_caps_at_six_words():
    name = derive_project_name("one two three four five six seven eight")
    assert name == "One Two Three Four Five Six"


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Build Me A Cli That", "build-me-a-cli-that"),
        ("Hello, World!", "hello-world"),
        ("!!!", "project"),
        ("", "project"),
    ],
)
def test_slugify_produces_valid_slug(name, expected):
    assert slugify(name) == expected


def test_slugify_truncates_long_names_without_trailing_hyphen():
    slug = slugify("word " * 30)
    assert len(slug) <= 56
    assert not slug.endswith("-")


def test_dedupe_id_returns_base_when_free():
    assert dedupe_id("my-project", set()) == "my-project"


def test_dedupe_id_appends_numeric_suffix_on_collision():
    existing = {"my-project", "my-project-2"}
    assert dedupe_id("my-project", existing) == "my-project-3"


def test_dedupe_name_returns_base_when_free():
    assert dedupe_name("My Project", set()) == "My Project"


def test_dedupe_name_is_case_insensitive():
    existing = {"my project"}
    assert dedupe_name("My Project", existing) == "My Project (2)"


def test_dedupe_name_appends_numeric_suffix_on_collision():
    existing = {"My Project", "My Project (2)"}
    assert dedupe_name("My Project", existing) == "My Project (3)"
