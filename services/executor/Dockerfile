# Foundry Executor service image (ARCHITECTURE.md §4.3). Build context is
# the repo root: docker compose build -> services/executor/Dockerfile.
#
# Installs this service's dependencies directly (pyproject.toml declares no
# [build-system]/package layout, and the repo's flat multi-package tree is
# deliberately not pip-installable) and runs the services/ tree via
# PYTHONPATH instead. Version floors mirror pyproject.toml's.
#
# Runs as root inside its own container for v1: this is an internal,
# non-agent-reachable service (only the Orchestrator can call it, and every
# agent workload runs in the separately-hardened workspace containers it
# spawns). The Docker *daemon* is only reachable through docker-socket-proxy
# (DOCKER_HOST, docker-compose.yml), never a mounted socket.

FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir \
    "pydantic>=2.9" \
    "docker>=7.1" \
    "fastapi>=0.115" \
    "uvicorn>=0.30" \
    "httpx>=0.27"

COPY services/__init__.py services/__init__.py
COPY services/executor/ services/executor/

ENV PYTHONPATH=/app

CMD ["python3", "-m", "services.executor.main"]
