# GENERATED FILE -- do not edit by hand. Rendered from
# images/workspace.template.Dockerfile by scripts/gen_workspace_images.py
# using config/stacks.yaml's "python" entry (ADR-045).
#
# Base image for ephemeral workspace containers spawned by the Executor
# (ARCHITECTURE.md 4.3). Every container built from this image runs
# non-root, with all capabilities dropped and no-new-privileges set by the
# Executor at container-creation time (services/executor/containers.py);
# this template carries the parts of least-privilege that belong at build
# time -- a non-root sandbox user at UID/GID 1000 and no leftover
# setuid/setgid binaries -- plus the Python3 + Playwright/Chromium layer
# the Executor's tools require in every image regardless of the project's
# own stack (ADR-028).

FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        git \
        curl \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir pytest pytest-cov playwright ruff

ENV PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright
RUN playwright install --with-deps chromium \
    && chmod -R a+rX /opt/ms-playwright

RUN find / -xdev -perm /6000 -type f -exec chmod a-s {} + 2>/dev/null || true

RUN groupadd --gid 1000 sandbox \
    && useradd --uid 1000 --gid sandbox --create-home --shell /usr/sbin/nologin sandbox

WORKDIR /workspace
USER 1000:1000
