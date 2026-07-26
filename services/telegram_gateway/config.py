"""Telegram Gateway runtime configuration, sourced from Docker secrets and env vars."""

import os
from pathlib import Path

from pydantic import BaseModel

from services.telegram_gateway.progress_poller import MAX_IDLE_POLLS, POLL_INTERVAL_SECONDS

DEFAULT_SECRETS_DIR = Path("/run/secrets")


class GatewayConfig(BaseModel):
    """Runtime configuration for the Telegram Gateway.

    Holds no business logic or memory of its own (ARCHITECTURE.md §4.1) --
    just the bot's own token, the single allowed user, and where to forward
    everything else.
    """

    bot_token: str
    allowed_user_id: int
    orchestrator_url: str
    poll_interval_seconds: float = POLL_INTERVAL_SECONDS
    max_idle_polls: int = MAX_IDLE_POLLS

    @classmethod
    def from_env(cls, secrets_dir: Path = DEFAULT_SECRETS_DIR) -> "GatewayConfig":
        """Builds a config from Docker secrets and environment variables.

        Args:
            secrets_dir: Directory containing Docker secret files. Defaults
                to /run/secrets (the real Docker Compose secrets mount);
                overridable so tests can point at a tmp_path instead.

        Returns:
            A validated GatewayConfig.

        Raises:
            FileNotFoundError: If a required secret file is missing.
            ValueError: If ORCHESTRATOR_URL is not set.
        """
        bot_token = (secrets_dir / "telegram_bot_token").read_text().strip()
        allowed_user_id = int((secrets_dir / "telegram_allowed_user_id").read_text().strip())
        orchestrator_url = os.environ.get("ORCHESTRATOR_URL")
        if not orchestrator_url:
            raise ValueError("ORCHESTRATOR_URL must be set")
        poll_interval_seconds = float(
            os.environ.get("PROGRESS_POLL_INTERVAL_SECONDS", POLL_INTERVAL_SECONDS)
        )
        max_idle_polls = int(os.environ.get("PROGRESS_POLL_MAX_IDLE_POLLS", MAX_IDLE_POLLS))
        return cls(
            bot_token=bot_token,
            allowed_user_id=allowed_user_id,
            orchestrator_url=orchestrator_url,
            poll_interval_seconds=poll_interval_seconds,
            max_idle_polls=max_idle_polls,
        )
