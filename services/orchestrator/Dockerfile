# Foundry Orchestrator service image (ARCHITECTURE.md §4.2). Build context
# is the repo root: docker compose build -> services/orchestrator/Dockerfile.
#
# Installs this service's dependencies directly (pyproject.toml declares no
# [build-system]/package layout, and the repo's flat multi-package tree is
# deliberately not pip-installable) and runs the services/ tree via
# PYTHONPATH instead. Version floors mirror pyproject.toml's.
#
# config/models.yaml is volume-mounted read-only at /app/config by
# docker-compose.yml (MODELS_CONFIG_PATH) rather than baked into the image,
# so swapping local <-> Claude routing stays a config edit, not a rebuild
# (CLAUDE.md "Model routing"). Runs as root inside its own container for v1:
# internal service, no agent-reachable surface (agents live in workspace
# containers the Executor spawns).

FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN pip install --no-cache-dir \
    "pydantic>=2.9" \
    "pyyaml>=6.0" \
    "httpx>=0.27" \
    "langgraph>=0.2" \
    "langgraph-checkpoint-sqlite>=2.0" \
    "langchain-core>=0.3" \
    "langchain-anthropic>=0.3" \
    "langchain-ollama>=0.2" \
    "langchain-openai>=0.2" \
    "fastapi>=0.115" \
    "uvicorn>=0.30" \
    "pygithub>=2.5"

COPY services/__init__.py services/__init__.py
COPY services/orchestrator/ services/orchestrator/

ENV PYTHONPATH=/app

CMD ["python3", "-m", "services.orchestrator.main"]
