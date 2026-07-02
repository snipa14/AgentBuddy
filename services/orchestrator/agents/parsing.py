"""Shared parsing helpers for agents that ask an LLM to respond in a fixed,
'## Heading' delimited structured-text format rather than free prose --
Planner, Coder, and (later) Reviewer/Tester all use this same shape because
model_router's CompletionRequest/CompletionResult (services/orchestrator/
model_router/schemas.py) is a plain text-completion contract with no native
tool-calling/structured-output support to lean on instead.
"""


def split_sections(content: str) -> dict[str, str]:
    """Splits a '## Heading' delimited response into {heading: body} pairs."""
    sections: dict[str, str] = {}
    current_heading: str | None = None
    current_lines: list[str] = []
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            if current_heading is not None:
                sections[current_heading] = "\n".join(current_lines)
            current_heading = stripped.removeprefix("## ").strip()
            current_lines = []
        elif current_heading is not None:
            current_lines.append(line)
    if current_heading is not None:
        sections[current_heading] = "\n".join(current_lines)
    return sections


def bullet_lines(section_body: str) -> list[str]:
    """Extracts '- ' bullet list items from a section's raw text body."""
    items = []
    for line in section_body.splitlines():
        stripped = line.strip()
        if stripped.startswith("- "):
            items.append(stripped.removeprefix("- ").strip())
    return items


def nonempty_lines(section_body: str) -> list[str]:
    """Extracts non-empty, non-bulleted lines from a section's raw text body."""
    return [line.strip() for line in section_body.splitlines() if line.strip()]
