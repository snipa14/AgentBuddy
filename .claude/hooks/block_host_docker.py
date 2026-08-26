"""
PreToolUse hook: blocks bash commands that bypass the docker-socket-proxy.

Reads the proposed bash command from stdin (Claude Code passes it as JSON),
exits with code 2 (block) if the command would touch the raw Docker socket
or invoke the docker CLI outside of docker compose / docker build contexts.
Exits with code 0 (allow) otherwise.

Claude Code hook contract:
  - stdin: JSON with key "tool_input" -> {"command": "<bash command>"}
  - exit 0: allow
  - exit 1: warn (allow but log)
  - exit 2: block (Claude sees the stderr message as the reason)
"""

import json
import re
import sys

BLOCKED_PATTERNS = [
    r"/var/run/docker\.sock",
    r"docker\s+(run|exec|start|stop|rm|kill|create)\s",
    r"docker\s+inspect\s",
    r"import\s+docker",
    r"DockerClient\(",
    r"from_env\(",
]

ALLOWED_DOCKER_PATTERNS = [
    r"docker\s+compose\s",
    r"docker\s+build\s",
    r"docker\s+images?\s",
    r"docker\s+ps\s",
    r"docker\s+logs\s",
]


def main() -> None:
    payload = json.load(sys.stdin)
    command = payload.get("tool_input", {}).get("command", "")

    for allowed in ALLOWED_DOCKER_PATTERNS:
        if re.search(allowed, command):
            sys.exit(0)

    for blocked in BLOCKED_PATTERNS:
        if re.search(blocked, command):
            print(
                f"[foundry-hook] BLOCKED: command appears to access Docker directly.\n"
                f"All Docker operations must go through the Executor service, which "
                f"talks to docker-socket-proxy. See ARCHITECTURE.md §4.3 and "
                f".claude/rules/security.md.\n"
                f"Blocked pattern: {blocked}",
                file=sys.stderr,
            )
            sys.exit(2)

    sys.exit(0)


if __name__ == "__main__":
    main()
