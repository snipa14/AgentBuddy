"""Constructs the Docker SDK client the Executor uses to reach docker-socket-proxy.

This module (and services/executor/ generally) is the only place in the
codebase, besides tests, allowed to import the `docker` package -- see
.claude/rules/security.md and .claude/hooks/guard_executor_imports.py.
"""

import docker


def build_docker_client() -> docker.DockerClient:
    """Builds a Docker SDK client from the environment.

    Honors the DOCKER_HOST environment variable, which in every real
    deployment points at docker-socket-proxy's HTTP endpoint (e.g.
    tcp://docker-socket-proxy:2375) rather than the raw host socket
    (ARCHITECTURE.md §4.3).
    """
    return docker.from_env()
