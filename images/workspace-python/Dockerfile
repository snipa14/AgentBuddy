# Base image for ephemeral workspace containers spawned by the Executor
# (ARCHITECTURE.md §4.3). Every container built from this image is expected
# to run non-root, with all capabilities dropped and no-new-privileges set
# by the Executor at container-creation time (services/executor/containers.py)
# -- this Dockerfile handles the parts of least-privilege that belong at
# build time: a non-root user and no leftover setuid/setgid binaries.

FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        git \
        curl \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir pytest pytest-cov playwright

# Headless Chromium + its OS-level dependencies, for tool_take_screenshot's
# real page-rendering step (Phase 12, ADR-024) -- fetching a URL's raw bytes
# and decoding them as an image file (the original approach) only works if
# the URL already serves an image; an actual HTML/CSS/JS page needs a real
# rendering engine. Installed to a world-readable path since the browser is
# launched at runtime by the non-root `sandbox` user created below, not by
# whichever user this RUN step executes as.
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright
RUN playwright install --with-deps chromium \
    && chmod -R a+rX /opt/ms-playwright

# Strip setuid/setgid bits from every binary in the image. cap_drop=["ALL"]
# at the container level already neutralizes these, but a build-time image
# shouldn't ship privilege-escalation binaries it doesn't need regardless of
# how it's later run.
RUN find / -xdev -perm /6000 -type f -exec chmod a-s {} + 2>/dev/null || true

RUN groupadd --gid 1000 sandbox \
    && useradd --uid 1000 --gid sandbox --create-home --shell /usr/sbin/nologin sandbox

WORKDIR /workspace
USER 1000:1000
