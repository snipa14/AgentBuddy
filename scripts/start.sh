#!/usr/bin/env bash
# Starts the Agent Buddy stack. Runs the secrets wizard first if any required
# secret is missing, then brings the Compose stack up.
#
# Usage:
#   ./scripts/start.sh               core services only
#   ./scripts/start.sh --local-llm   core services + Ollama (RTX 3090 profile)
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

cat <<'BANNER'
    _       ____   _____   _   _   _____      ____    _   _   ____    ____   __   __
   / \     / ___| | ____| | \ | | |_   _|    | __ )  | | | | |  _ \  |  _ \  \ \ / /
  / _ \   | |  _  |  _|   |  \| |   | |      |  _ \  | | | | | | | | | | | |  \ V /
 / ___ \  | |_| | | |___  | |\  |   | |      | |_) | | |_| | | |_| | | |_| |   | |
/_/   \_\  \____| |_____| |_| \_|   |_|      |____/   \___/  |____/  |____/    |_|
BANNER
echo "[start] Waking up Agent Buddy..."

# setup.sh's own prompt_secret calls are the single source of truth for
# which secrets are required -- it's idempotent (skips any secret that
# already has a file), so it's safe to always run rather than keeping a
# second, separately-maintained list of secret names here that could drift
# out of sync with setup.sh's actual prompt sequence.
"$ROOT_DIR/scripts/setup.sh"

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

# The three workspace images are built, not part of the long-lived Compose
# stack (they're what the Executor launches short-lived task containers
# from), so `docker compose up` alone never produces them -- skipping this
# left a first-timer's stack looking healthy right up until their first
# /new failed deep inside the Coder phase with a bare "no such image"
# error, several steps removed from anything that looks like a setup
# problem. Idempotent: a no-op once an image has been built.
declare -A workspace_images=(
  ["foundry/workspace-python:3.12"]="images/workspace-python"
  ["foundry/workspace-node:20"]="images/workspace-node"
  ["foundry/workspace-go:1.22"]="images/workspace-go"
)
for image in "${!workspace_images[@]}"; do
  if ! docker image inspect "$image" >/dev/null 2>&1; then
    echo "[start] Building workspace image $image (first run only)..."
    docker build -t "$image" "$ROOT_DIR/${workspace_images[$image]}"
  fi
done

compose_files=(-f docker-compose.yml)
if [[ "${1:-}" == "--local-llm" ]]; then
  compose_files+=(-f docker-compose.local-llm.yml)
fi

docker compose "${compose_files[@]}" up -d

_all_healthy() {
  local service
  for service in "$@"; do
    docker compose "${compose_files[@]}" ps "$service" 2>/dev/null | grep -q "(healthy)" || return 1
  done
  return 0
}

# Only these three services declare a healthcheck (see docker-compose.yml);
# telegram-gateway has none to poll for. `up -d` returns the moment
# containers are *started*, not once they're actually serving -- printing
# "up" right after that left a build failure or crash loop looking
# identical to success, with Telegram just never responding and no pointer
# to where to look.
echo "[start] Waiting for services to report healthy (up to 90s)..."
healthchecked_services=(docker-socket-proxy executor orchestrator)
deadline=$((SECONDS + 90))
until _all_healthy "${healthchecked_services[@]}" || (( SECONDS >= deadline )); do
  sleep 3
done

if _all_healthy "${healthchecked_services[@]}"; then
  echo "[start] Agent Buddy is up. Message your bot on Telegram to begin."
else
  echo "[start] Stack started, but not every service reported healthy within 90s."
  echo "[start] Check what's slow with 'docker compose ps' or './foundry logs <service>' before assuming something's wrong -- first boot can take longer."
fi
