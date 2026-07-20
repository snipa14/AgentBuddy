# Base image for ephemeral Node.js workspace containers spawned by the
# Executor (ARCHITECTURE.md §4.3, ADR-028). Mirrors
# images/workspace-python/Dockerfile's security posture exactly -- a
# non-root user, all capabilities dropped and no-new-privileges set by the
# Executor at container-creation time (services/executor/containers.py),
# and no leftover setuid/setgid binaries. Polyglot support (Phase 17) adds
# *images*, one per stack -- it deliberately does not add any agent-reachable
# way to build or customize an image (ADR-028).

FROM node:20-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        git \
        curl \
        python3 \
        python3-pip \
    && rm -rf /var/lib/apt/lists/*

# tool_list_files and tool_take_screenshot (services/executor/tools.py) both
# run a Python script inside the workspace container regardless of the
# project's own stack -- so every workspace image, node included, needs a
# real Python3 + Playwright/Chromium available, not just the tools a Node
# project's own code would use.
RUN pip install --break-system-packages --no-cache-dir playwright

ENV PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright
RUN playwright install --with-deps chromium \
    && chmod -R a+rX /opt/ms-playwright

# Strip setuid/setgid bits from every binary in the image. cap_drop=["ALL"]
# at the container level already neutralizes these, but a build-time image
# shouldn't ship privilege-escalation binaries it doesn't need regardless of
# how it's later run.
RUN find / -xdev -perm /6000 -type f -exec chmod a-s {} + 2>/dev/null || true

# node:20-slim already ships a `node` user/group at UID/GID 1000, so a plain
# groupadd/useradd (the python image's approach) fails on the collision --
# rename the existing pair to `sandbox` instead, keeping UID/GID 1000 (which
# services/executor/containers.py's WORKSPACE_UID assumes).
RUN groupmod --new-name sandbox node \
    && usermod --login sandbox --home /home/sandbox --move-home \
       --shell /usr/sbin/nologin node

WORKDIR /workspace
USER 1000:1000
