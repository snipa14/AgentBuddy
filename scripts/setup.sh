#!/usr/bin/env bash
# First-run secrets wizard. Prompts only for secrets that don't already
# have a file under secrets/, so re-running this script is a no-op once
# everything is set.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SECRETS_DIR="$ROOT_DIR/secrets"
mkdir -p "$SECRETS_DIR"

prompt_secret() {
  # pattern is a bash extended-regex the value must fully match, checked with
  # [[ =~ ]] -- defaults to accepting anything non-empty. Kept loose (a
  # minimum length, not an exact prefix) for the API key/token fields: a
  # provider's own valid formats vary and can change, so this only exists to
  # catch an obviously truncated or wrong-field paste before it silently
  # fails deep inside a live API call later.
  local name="$1" label="$2" silent="$3" pattern="${4:-^.+$}"
  local path="$SECRETS_DIR/$name"

  if [[ -f "$path" ]]; then
    echo "[setup] $name already set, skipping."
    return 0
  fi

  local value=""
  while true; do
    if [[ "$silent" == "silent" ]]; then
      read -rsp "[setup] $label: " value
      echo
    else
      read -rp "[setup] $label: " value
    fi
    if [[ -z "$value" ]]; then
      continue
    fi
    if [[ ! "$value" =~ $pattern ]]; then
      echo "[setup] That doesn't look like a valid $label -- try again."
      continue
    fi
    break
  done

  printf '%s' "$value" > "$path"
  chmod 600 "$path"
  echo "[setup] Saved $name."
}

prompt_secret "telegram_bot_token" "Telegram bot token (from @BotFather)" silent
prompt_secret "telegram_allowed_user_id" "Your Telegram numeric user ID" plain '^[0-9]+$'
prompt_secret "claude_api_key" "Claude API key" silent '^.{20,}$'
prompt_secret "github_token" "GitHub personal access token" silent '^.{20,}$'

echo "[setup] Done. Secrets are stored in $SECRETS_DIR (mode 600, gitignored)."
