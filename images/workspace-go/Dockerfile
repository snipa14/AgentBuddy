# GENERATED FILE -- do not edit by hand. Rendered from
# images/workspace.template.Dockerfile by scripts/gen_workspace_images.py
# using config/stacks.yaml's "go" entry (ADR-045).
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

FROM golang:1.22-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        git \
        curl \
        python3 \
        python3-pip \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --break-system-packages --no-cache-dir playwright

ENV PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright
RUN playwright install --with-deps chromium \
    && chmod -R a+rX /opt/ms-playwright

ENV GOCACHE=/home/sandbox/.cache/go-build
ENV GOMODCACHE=/home/sandbox/go/pkg/mod

RUN find / -xdev -perm /6000 -type f -exec chmod a-s {} + 2>/dev/null || true

RUN groupadd --gid 1000 sandbox \
    && useradd --uid 1000 --gid sandbox --create-home --shell /usr/sbin/nologin sandbox

WORKDIR /workspace
USER 1000:1000
