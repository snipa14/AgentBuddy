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

compose_files=(-f docker-compose.yml)
if [[ "${1:-}" == "--local-llm" ]]; then
  compose_files+=(-f docker-compose.local-llm.yml)
fi

docker compose "${compose_files[@]}" up -d
