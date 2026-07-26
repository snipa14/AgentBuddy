#!/usr/bin/env bash
# Starts the Foundry stack. Runs the secrets wizard first if any required
# secret is missing, then brings the Compose stack up.
#
# Usage:
#   ./scripts/start.sh               core services only
#   ./scripts/start.sh --local-llm   core services + Ollama (RTX 3090 profile)
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

REQUIRED_SECRETS=(telegram_bot_token telegram_allowed_user_id claude_api_key github_token)
for name in "${REQUIRED_SECRETS[@]}"; do
  if [[ ! -f "secrets/$name" ]]; then
    "$ROOT_DIR/scripts/setup.sh"
    break
  fi
done

# docker-compose.yml hard-requires PROJECTS_HOST_PATH (the Executor needs the
# *host* absolute path of projects/ -- see services/executor/SECURITY.md), so
# a fresh clone must get a .env before `docker compose up` can succeed.
if [[ ! -f .env ]]; then
  mkdir -p projects
  {
    echo "COMPOSE_PROJECT_NAME=foundry"
    echo "PROJECTS_HOST_PATH=$ROOT_DIR/projects"
  } > .env
  echo "[start] Wrote .env with PROJECTS_HOST_PATH=$ROOT_DIR/projects"
fi

# foundry_workspace is declared external in docker-compose.yml (isolated
# network the Executor joins workspace containers to at runtime, per
# .claude/rules/security.md) -- it's referenced by no service, so Compose
# won't create it on its own. Idempotent: a no-op if it already exists.
if ! docker network inspect foundry_workspace >/dev/null 2>&1; then
  docker network create --driver bridge foundry_workspace
  echo "[start] Created foundry_workspace network"
fi

compose_files=(-f docker-compose.yml)
if [[ "${1:-}" == "--local-llm" ]]; then
  compose_files+=(-f docker-compose.local-llm.yml)
fi

docker compose "${compose_files[@]}" up -d
