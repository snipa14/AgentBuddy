"""Foundry Telegram Gateway: the only service that talks to the human side.

A thin, stateless bot (ARCHITECTURE.md §4.1). Authenticates the single
allowed Telegram user, forwards everything else to the Orchestrator's HTTP
API, and renders approval requests as inline-keyboard messages. Holds no
business logic, memory, or credentials beyond its own bot token.

Deliberately docstring-only: eagerly re-exporting submodules here forced
`aiogram` onto every import under this package, including aiogram-free
modules like `orchestrator_client` and their tests -- the same bug class
`services/orchestrator/__init__.py` (Phase 6) and
`services/orchestrator/agents/__init__.py` (Phase 17) each had to fix once
already. Import submodules directly instead.
"""
