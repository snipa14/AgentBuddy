"""Foundry Telegram Gateway: the only service that talks to the human side.

A thin, stateless bot (ARCHITECTURE.md §4.1). Authenticates the single
allowed Telegram user, forwards everything else to the Orchestrator's HTTP
API, and renders approval requests as inline-keyboard messages. Holds no
business logic, memory, or credentials beyond its own bot token.
"""

from services.telegram_gateway.auth import AllowedUserMiddleware
from services.telegram_gateway.config import GatewayConfig
from services.telegram_gateway.orchestrator_client import OrchestratorClient
from services.telegram_gateway.rendering import render_approval

__all__ = [
    "AllowedUserMiddleware",
    "GatewayConfig",
    "OrchestratorClient",
    "render_approval",
]
