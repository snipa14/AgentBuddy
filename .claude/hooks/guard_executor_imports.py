"""
PreToolUse hook: blocks Docker SDK imports outside services/executor/.

The Docker SDK may only be imported inside services/executor/. If any other
file tries to import it, the write is blocked. This enforces the architectural
rule that the Executor is the sole owner of the Docker control surface.

Claude Code hook contract:
  - stdin: JSON with key "tool_input" containing the write payload
  - exit 0: allow
  - exit 2: block
"""

import json
import re
import sys

DOCKER_IMPORT_PATTERNS = [
    r"^\s*import\s+docker\b",
    r"^\s*from\s+docker\b",
    r"docker\.from_env\(",
    r"DockerClient\(",
    r"docker\.APIClient\(",
]

EXECUTOR_PATH_PREFIX = "services/executor/"


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

    if EXECUTOR_PATH_PREFIX in path:
        sys.exit(0)

    if path.endswith("_test.py") or "/tests/" in path:
        sys.exit(0)

    for pattern in DOCKER_IMPORT_PATTERNS:
        if re.search(pattern, content, re.MULTILINE):
            print(
                f"[foundry-hook] BLOCKED: Docker SDK import detected in '{path}'.\n"
                "The Docker SDK may only be imported inside services/executor/.\n"
                "If this feature needs Docker access, it must go through the "
                "Executor's typed tool API. See ARCHITECTURE.md §4.3 and "
                ".claude/rules/security.md.",
                file=sys.stderr,
            )
            sys.exit(2)

    sys.exit(0)


if __name__ == "__main__":
    main()
