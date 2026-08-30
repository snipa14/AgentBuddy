"""Derives a human-readable project name and a unique slug id from free text.

Backs the Telegram `/new` flow (ARCHITECTURE.md §4.6): unlike the CLI
`new-project` command, a chat message has no explicit `--name`/project_id,
so this module turns the message itself into both.
"""

import re

from services.orchestrator.registry.validation import PROJECT_ID_PATTERN

_COMMAND_PREFIX_PATTERN = re.compile(r"^/\S+\s*")
_WORD_PATTERN = re.compile(r"[A-Za-z0-9]+")
_MAX_NAME_WORDS = 6
_MAX_SLUG_LENGTH = 56
"""Leaves room for a "-NN" dedupe suffix under PROJECT_ID_PATTERN's 64-char cap."""

_FALLBACK_NAME = "Untitled Project"
_FALLBACK_SLUG = "project"


def derive_project_name(text: str) -> str:
    """Builds a human-readable project name from a chat message.

    Args:
        text: The raw message text, e.g. "/new Build me a CLI that converts
            CSV to JSON" -- a leading slash-command token is stripped before
            the name is derived.

    Returns:
        The first few words of `text`, title-cased (e.g. "Build Me A Cli
        That"), or "Untitled Project" if no word characters remain.
    """
    stripped = _COMMAND_PREFIX_PATTERN.sub("", text).strip()
    words = _WORD_PATTERN.findall(stripped)[:_MAX_NAME_WORDS]
    if not words:
        return _FALLBACK_NAME
    return " ".join(word.capitalize() for word in words)


def slugify(name: str) -> str:
    """Converts a project name into a candidate project_id slug.

    Args:
        name: A human-readable project name, e.g. "Build Me A Cli That".

    Returns:
        A lowercase, hyphen-separated slug matching PROJECT_ID_PATTERN
        (e.g. "build-me-a-cli-that"), truncated to _MAX_SLUG_LENGTH and
        with trailing hyphens trimmed after truncation, or "project" if
        nothing alphanumeric remains.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    slug = slug[:_MAX_SLUG_LENGTH].rstrip("-")
    if not slug or not PROJECT_ID_PATTERN.match(slug):
        return _FALLBACK_SLUG
    return slug


def dedupe_id(base_slug: str, existing_ids: set[str]) -> str:
    """Appends a numeric suffix to base_slug until it's not in existing_ids.

    Args:
        base_slug: The candidate project_id slug.
        existing_ids: Every project_id already in use.

    Returns:
        base_slug unchanged if it's free, else "<base_slug>-2",
        "<base_slug>-3", etc, the first one not already taken.
    """
    if base_slug not in existing_ids:
        return base_slug
    suffix = 2
    while f"{base_slug}-{suffix}" in existing_ids:
        suffix += 1
    return f"{base_slug}-{suffix}"


def dedupe_name(base_name: str, existing_names: set[str]) -> str:
    """Appends a numeric suffix to base_name until it's not in existing_names.

    Args:
        base_name: The candidate human-readable name.
        existing_names: Every project name already in use, compared
            case-insensitively.

    Returns:
        base_name unchanged if it's free, else "<base_name> (2)",
        "<base_name> (3)", etc, the first one not already taken.
    """
    lowered_existing = {existing.lower() for existing in existing_names}
    if base_name.lower() not in lowered_existing:
        return base_name
    suffix = 2
    while f"{base_name} ({suffix})".lower() in lowered_existing:
        suffix += 1
    return f"{base_name} ({suffix})"
