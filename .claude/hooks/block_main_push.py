"""
PreToolUse hook: blocks any git operation targeting main without a recorded approval.

The merge-to-main gate lives in the LangGraph graph as an interrupt() node.
This hook is the fallback layer: if Claude attempts a direct push or merge to
main via bash (bypassing the graph), it is blocked here unconditionally.

Claude Code hook contract:
  - stdin: JSON with key "tool_input" -> {"command": "<bash command>"}
  - exit 0: allow
  - exit 2: block
"""

import json
import re
import sys

BLOCKED_PATTERNS = [
    r"git\s+push\s+.*\bmain\b",
    r"git\s+push\s+.*\bmaster\b",
    r"git\s+merge\s+.*\bmain\b",
    r"git\s+merge\s+.*\bmaster\b",
    r"git\s+push\s+--force",
    r"git\s+push\s+-f\b",
]


def main() -> None:
    payload = json.load(sys.stdin)
    command = payload.get("tool_input", {}).get("command", "")

    for pattern in BLOCKED_PATTERNS:
        if re.search(pattern, command):
            print(
                "[foundry-hook] BLOCKED: direct push or merge to main is not permitted.\n"
                "Merge-to-main requires a Telegram approval through the LangGraph "
                "interrupt() gate. See .claude/rules/git.md and ARCHITECTURE.md §6.3.",
                file=sys.stderr,
            )
            sys.exit(2)

    sys.exit(0)


if __name__ == "__main__":
    main()
