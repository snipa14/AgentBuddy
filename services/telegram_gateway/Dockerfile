# Foundry Telegram Gateway service image (ARCHITECTURE.md §4.1). Build
# context is the repo root: docker compose build ->
# services/telegram_gateway/Dockerfile.
#
# Installs this service's dependencies directly (pyproject.toml declares no
# [build-system]/package layout, and the repo's flat multi-package tree is
# deliberately not pip-installable) and runs the services/ tree via
# PYTHONPATH instead. Version floors mirror pyproject.toml's.
#
# Runs as root inside its own container for v1: internal service holding
# only its own bot token (via Docker secrets), no agent-reachable surface.

FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir \
    "pydantic>=2.9" \
    "httpx>=0.27" \
    "aiogram>=3.15"

COPY services/__init__.py services/__init__.py
COPY services/telegram_gateway/ services/telegram_gateway/

ENV PYTHONPATH=/app

CMD ["python3", "-m", "services.telegram_gateway.main"]
