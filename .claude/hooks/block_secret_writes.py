"""
PreToolUse hook: scans proposed file writes for credential patterns.

Runs before Write, Edit, and MultiEdit tool calls. Checks the content
to be written against a set of known secret patterns. Blocks the write
and asks Claude to store credentials via Docker secrets instead.

Claude Code hook contract:
  - stdin: JSON with key "tool_input" containing the write payload
  - exit 0: allow
  - exit 2: block
"""

import json
import re
import sys

SECRET_PATTERNS = [
    (r"sk-ant-[a-zA-Z0-9\-_]{20,}", "Anthropic API key"),
    (r"sk-[a-zA-Z0-9]{20,}", "OpenAI-style API key"),
    (r"ghp_[a-zA-Z0-9]{36}", "GitHub personal access token"),
    (r"ghs_[a-zA-Z0-9]{36}", "GitHub app token"),
    (r"\d{9,10}:[a-zA-Z0-9_\-]{35}", "Telegram bot token"),
    (r"-----BEGIN (RSA|EC|OPENSSH) PRIVATE KEY-----", "Private key block"),
    (r"AKIA[0-9A-Z]{16}", "AWS access key ID"),
]

ALLOWED_PATHS = [
    r"secrets/.*\.example$",
    r"\.env\.example$",
    r"tests/fixtures/",
    r"ARCHITECTURE\.md$",
    r"CLAUDE\.md$",
    r"\.claude/",
]


def extract_content(payload: dict) -> str:
    tool_input = payload.get("tool_input", {})
    return (
        tool_input.get("content", "")
        or tool_input.get("new_str", "")
        or tool_input.get("new_string", "")
        or ""
    )


def extract_path(payload: dict) -> str:
    tool_input = payload.get("tool_input", {})
    return tool_input.get("path", "") or tool_input.get("file_path", "") or ""


def main() -> None:
    payload = json.load(sys.stdin)
    path = extract_path(payload)
    content = extract_content(payload)

    for allowed in ALLOWED_PATHS:
        if re.search(allowed, path):
            sys.exit(0)

    for pattern, label in SECRET_PATTERNS:
        if re.search(pattern, content):
            print(
                f"[foundry-hook] BLOCKED: potential {label} detected in file write to '{path}'.\n"
                "Credentials must be stored in secrets/ (via Docker secrets) and "
                "referenced by name. Never write a real credential into source files.\n"
                "See ARCHITECTURE.md §8 and .claude/rules/security.md.",
                file=sys.stderr,
            )
            sys.exit(2)

    sys.exit(0)


if __name__ == "__main__":
    main()
