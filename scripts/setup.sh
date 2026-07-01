#!/usr/bin/env bash
# First-run secrets wizard. Prompts only for secrets that don't already
# have a file under secrets/, so re-running this script is a no-op once
# everything is set.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SECRETS_DIR="$ROOT_DIR/secrets"
mkdir -p "$SECRETS_DIR"

prompt_secret() {
  local name="$1" label="$2" silent="$3"
  local path="$SECRETS_DIR/$name"

  if [[ -f "$path" ]]; then
    echo "[setup] $name already set, skipping."
    return 0
  fi

  local value=""
  while [[ -z "$value" ]]; do
    if [[ "$silent" == "silent" ]]; then
      read -rsp "[setup] $label: " value
      echo
    else
      read -rp "[setup] $label: " value
    fi
  done

  printf '%s' "$value" > "$path"
  chmod 600 "$path"
  echo "[setup] Saved $name."
}

prompt_secret "telegram_bot_token" "Telegram bot token (from @BotFather)" silent
prompt_secret "telegram_allowed_user_id" "Your Telegram numeric user ID" plain
prompt_secret "claude_api_key" "Claude API key" silent
prompt_secret "github_token" "GitHub personal access token" silent

echo "[setup] Done. Secrets are stored in $SECRETS_DIR (mode 600, gitignored)."
