"""Unit test for build_docker_client()."""

from unittest.mock import MagicMock

from services.executor.docker_client import build_docker_client


def test_build_docker_client_returns_docker_from_env_result(monkeypatch):
    sentinel_client = MagicMock()
    monkeypatch.setattr("docker.from_env", lambda: sentinel_client)

    assert build_docker_client() is sentinel_client
