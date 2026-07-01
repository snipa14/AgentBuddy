"""
PreToolUse hook: blocks package managers running on the host.

All dependency installs happen inside workspace containers via
Executor.tool_run_command(). Nothing is installed on the host.

This hook allows installs that are clearly scoped inside a container
context (e.g., via `docker exec` or `docker run`) but blocks bare
invocations of pip, apt, brew, npm -g, cargo, etc.

Claude Code hook contract:
  - stdin: JSON with key "tool_input" -> {"command": "<bash command>"}
  - exit 0: allow
  - exit 2: block
"""

import json
import re
import sys

BLOCKED_PATTERNS = [
    r"\bpip\s+install\b",
    r"\bpip3\s+install\b",
    r"\buv\s+pip\s+install\b",
    r"\bapt\s+install\b",
    r"\bapt-get\s+install\b",
    r"\bbrew\s+install\b",
    r"\bnpm\s+install\s+-g\b",
    r"\bnpm\s+i\s+-g\b",
    r"\bcargo\s+install\b",
    r"\bgo\s+install\b",
]

ALLOWED_CONTEXTS = [
    r"docker\s+exec",
    r"docker\s+run",
]


def main() -> None:
    payload = json.load(sys.stdin)
    command = payload.get("tool_input", {}).get("command", "")

    for context in ALLOWED_CONTEXTS:
        if re.search(context, command):
            sys.exit(0)

    for pattern in BLOCKED_PATTERNS:
        if re.search(pattern, command):
            print(
                "[foundry-hook] BLOCKED: host-level package install detected.\n"
                "All installs must happen inside workspace containers via "
                "Executor.tool_run_command(). The host must only have Docker "
                "installed. See ARCHITECTURE.md §8 (Security Model).",
                file=sys.stderr,
            )
            sys.exit(2)

    sys.exit(0)


if __name__ == "__main__":
    main()
