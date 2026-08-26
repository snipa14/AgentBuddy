# GENERATED FILE -- do not edit by hand. Rendered from
# images/workspace.template.Dockerfile by scripts/gen_workspace_images.py
# using config/stacks.yaml's "$stack_name" entry (ADR-045).
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

FROM $base_image

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
$apt_package_lines
    && rm -rf /var/lib/apt/lists/*

RUN pip install$pip_flags --no-cache-dir $pip_packages

ENV PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright
RUN playwright install --with-deps chromium \
    && chmod -R a+rX /opt/ms-playwright
$env_lines
RUN find / -xdev -perm /6000 -type f -exec chmod a-s {} + 2>/dev/null || true

$sandbox_user_block
$cache_dir_setup
WORKDIR /workspace
USER 1000:1000
