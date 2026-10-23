"""Integration test proving docker-socket-proxy's whitelist actually holds.

Requires a real Docker daemon (docker-socket-proxy's production config lives
in docker-compose.yml -- this starts just that one service, never the full
stack, so it needs no secrets/.env). Skipped cleanly if the daemon, the
`docker` CLI/compose, or the network-connect this needs aren't available in
the current sandbox.

CLI availability note: the original design for this test shelled out to the
`docker` CLI (`docker compose up -d ...`, `docker network connect ...`).
This sandbox has a real, reachable Docker daemon (via a bind-mounted socket)
but no `docker` CLI binary at all -- only the Python `docker` SDK package is
installed (confirmed with `shutil.which("docker")` below). Rather than skip
outright when the CLI is missing, the fixtures below fall back to driving
the same operations through the Docker SDK:

  - `running_proxy` parses docker-socket-proxy's actual service definition
    (image, environment, volumes) straight out of docker-compose.yml with
    `yaml.safe_load` -- not a hand-retyped copy of the whitelist -- and
    replays it via `containers.create`/`start`, so this still proves the
    real production config holds, not a substitute one.
  - `proxy_reachable_from_this_process` replaces `docker network connect`
    with `Network.connect()`, registering this test process's own
    container under the `docker-socket-proxy` network alias that compose
    would otherwise assign from the service name, so `proxy_client`'s
    `tcp://docker-socket-proxy:2375` base_url resolves exactly as it would
    against a real compose-managed deployment.

When the `docker` CLI *is* present, the fixtures use it directly, matching
the original subprocess-based design -- which is what GitHub Actions runners
do, since they ship both a daemon and the CLI. Those branches therefore run
for the first time in CI and must not assume this sandbox's own layout: the
compose invocations below resolve their working directory from REPO_ROOT
rather than a hardcoded path.

The SDK-fallback branches never touch PROXY_CONTAINER_NAME/PROXY_NETWORK_NAME
(the real names a docker-compose.yml-managed production deployment would
use) -- they operate exclusively on FALLBACK_CONTAINER_NAME/
FALLBACK_NETWORK_NAME, a private sandbox this fixture owns outright and
tears down (stop + remove) after the module's tests finish. The CLI branch
targets the real deployment on purpose and only stops what it itself
started, leaving an already-running production proxy untouched.

Run with: pytest -m integration services/executor/tests/integration/test_proxy_boundary.py
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from docker import errors as docker_errors
from docker.client import DockerClient

from services.executor.docker_client import build_docker_client

PROXY_CONTAINER_NAME = "foundry-docker-socket-proxy"
PROXY_NETWORK_NAME = "foundry_internal"
PROXY_SERVICE_ALIAS = "docker-socket-proxy"
PROXY_PORT = 2375

# Used only by the SDK-fallback branches below (no `docker` CLI available).
# Deliberately distinct from PROXY_CONTAINER_NAME/PROXY_NETWORK_NAME, which
# are the real names a docker-compose.yml-managed production deployment
# would use -- reusing those names here would risk this fixture adopting
# and mutating a real, already-running production proxy container/network
# on a host where the CLI happens to be unavailable. The CLI branch tests
# the real deployment on purpose; the SDK-fallback branch needs its own
# private sandbox instead.
FALLBACK_CONTAINER_NAME = "foundry-docker-socket-proxy-sdk-fallback-test"
FALLBACK_NETWORK_NAME = "foundry_internal_sdk_fallback_test"
REPO_ROOT = Path(__file__).resolve().parents[4]
COMPOSE_FILE_PATH = REPO_ROOT / "docker-compose.yml"

DOCKER_CLI_AVAILABLE = shutil.which("docker") is not None

pytestmark = pytest.mark.integration


def _load_proxy_service_spec() -> dict:
    """Parses docker-socket-proxy's real service block out of docker-compose.yml.

    This is used only by the SDK fallback path (no `docker` CLI/compose
    available), so that the fallback still exercises the production
    whitelist as actually declared in docker-compose.yml rather than a
    value re-typed by hand into this test file.

    Returns:
        The `services.docker-socket-proxy` mapping from docker-compose.yml
        (image, container_name, environment, volumes, restart, ...).

    Raises:
        FileNotFoundError: If docker-compose.yml is missing.
        KeyError: If the docker-socket-proxy service has been renamed or
            removed from docker-compose.yml.
    """
    compose = yaml.safe_load(COMPOSE_FILE_PATH.read_text())
    return compose["services"]["docker-socket-proxy"]


@pytest.fixture(scope="module")
def daemon_client():
    try:
        client = build_docker_client()
        client.ping()
    except docker_errors.DockerException as exc:
        pytest.skip(f"no reachable Docker daemon: {exc}")
    return client


@pytest.fixture(scope="module")
def running_proxy(daemon_client):
    if DOCKER_CLI_AVAILABLE:
        try:
            already_running = daemon_client.containers.get(PROXY_CONTAINER_NAME).status == "running"
        except docker_errors.NotFound:
            already_running = False

        up = subprocess.run(
            ["docker", "compose", "up", "-d", "docker-socket-proxy"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        if up.returncode != 0:
            pytest.skip(f"could not start docker-socket-proxy via compose: {up.stderr}")
        try:
            daemon_client.containers.get(PROXY_CONTAINER_NAME).reload()
        except docker_errors.NotFound:
            pytest.skip("docker-socket-proxy container not found after compose up")

        yield PROXY_CONTAINER_NAME

        if not already_running:
            stop = subprocess.run(
                ["docker", "compose", "stop", "docker-socket-proxy"],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
            )
            if stop.returncode != 0:
                print(
                    "proxy-boundary teardown: could not stop docker-socket-proxy "
                    f"via compose: {stop.stderr}"
                )
        return

    try:
        spec = _load_proxy_service_spec()
    except (FileNotFoundError, KeyError) as exc:
        pytest.skip(f"could not read docker-socket-proxy service spec: {exc}")

    try:
        network = daemon_client.networks.get(FALLBACK_NETWORK_NAME)
    except docker_errors.NotFound:
        network = daemon_client.networks.create(FALLBACK_NETWORK_NAME, driver="bridge")

    try:
        container = daemon_client.containers.get(FALLBACK_CONTAINER_NAME)
    except docker_errors.NotFound:
        volume_spec = spec["volumes"][0]
        host_path, container_path, mode = volume_spec.split(":")
        try:
            daemon_client.images.pull(spec["image"])
        except docker_errors.APIError as exc:
            pytest.skip(f"could not pull {spec['image']}: {exc}")
        try:
            container = daemon_client.containers.create(
                spec["image"],
                name=FALLBACK_CONTAINER_NAME,
                environment={str(k): str(v) for k, v in spec["environment"].items()},
                volumes={host_path: {"bind": container_path, "mode": mode}},
                detach=True,
            )
        except docker_errors.APIError as exc:
            pytest.skip(f"could not create docker-socket-proxy via Docker SDK: {exc}")

    container.reload()
    if FALLBACK_NETWORK_NAME not in container.attrs["NetworkSettings"]["Networks"]:
        try:
            network.connect(container, aliases=[PROXY_SERVICE_ALIAS])
        except docker_errors.APIError as exc:
            pytest.skip(f"could not connect docker-socket-proxy to {FALLBACK_NETWORK_NAME}: {exc}")

    container.reload()
    if container.status != "running":
        try:
            container.start()
        except docker_errors.APIError as exc:
            pytest.skip(f"could not start docker-socket-proxy via Docker SDK: {exc}")

    yield FALLBACK_CONTAINER_NAME

    # This fixture owns this container exclusively (it's a test-scoped name
    # that can never collide with a real deployment), so stopping and
    # removing it unconditionally is always safe.
    try:
        container.reload()
        container.stop()
    except docker_errors.APIError as exc:
        print(f"proxy-boundary teardown: could not stop {FALLBACK_CONTAINER_NAME}: {exc}")
    try:
        container.remove(force=True)
    except docker_errors.APIError as exc:
        print(f"proxy-boundary teardown: could not remove {FALLBACK_CONTAINER_NAME}: {exc}")
    try:
        daemon_client.networks.get(FALLBACK_NETWORK_NAME).remove()
    except docker_errors.NotFound:
        pass
    except docker_errors.APIError as exc:
        print(f"proxy-boundary teardown: could not remove {FALLBACK_NETWORK_NAME}: {exc}")


@pytest.fixture(scope="module")
def proxy_reachable_from_this_process(running_proxy, daemon_client):
    self_id = os.environ.get("HOSTNAME")
    if not self_id:
        pytest.skip(
            "HOSTNAME env var not set -- can't identify this container to network-connect it"
        )

    if DOCKER_CLI_AVAILABLE:
        connect = subprocess.run(
            ["docker", "network", "connect", PROXY_NETWORK_NAME, self_id],
            capture_output=True,
            text=True,
        )
        if connect.returncode != 0 and "already exists" not in connect.stderr:
            pytest.skip(f"could not join {PROXY_NETWORK_NAME}: {connect.stderr}")
        yield
        disconnect = subprocess.run(
            ["docker", "network", "disconnect", PROXY_NETWORK_NAME, self_id],
            capture_output=True,
            text=True,
        )
        if disconnect.returncode != 0:
            print(
                f"proxy-boundary teardown: could not disconnect {self_id} from "
                f"{PROXY_NETWORK_NAME}: {disconnect.stderr}"
            )
        return

    try:
        network = daemon_client.networks.get(FALLBACK_NETWORK_NAME)
        network.connect(self_id)
    except docker_errors.APIError as exc:
        if "already exists" not in str(exc):
            pytest.skip(f"could not join {FALLBACK_NETWORK_NAME} via Docker SDK: {exc}")
    yield
    try:
        network = daemon_client.networks.get(FALLBACK_NETWORK_NAME)
        network.disconnect(self_id)
    except docker_errors.APIError as exc:
        print(
            f"proxy-boundary teardown: could not disconnect {self_id} from "
            f"{FALLBACK_NETWORK_NAME}: {exc}"
        )


@pytest.fixture
def proxy_client(proxy_reachable_from_this_process):
    try:
        client = DockerClient(base_url=f"tcp://{PROXY_SERVICE_ALIAS}:{PROXY_PORT}")
        client.ping()
    except docker_errors.DockerException as exc:
        pytest.skip(f"proxy not reachable by hostname from this process: {exc}")
    return client


def test_proxy_denies_networks_list(proxy_client):
    with pytest.raises(docker_errors.APIError) as exc_info:
        proxy_client.networks.list()

    assert exc_info.value.response.status_code == 403


def test_proxy_denies_image_build(proxy_client, tmp_path):
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM scratch\n")

    with pytest.raises(docker_errors.APIError) as exc_info:
        proxy_client.images.build(path=str(tmp_path), tag="proxy-boundary-should-never-exist")

    assert exc_info.value.response.status_code == 403


def test_proxy_allows_containers_list(proxy_client):
    result = proxy_client.containers.list(all=True)

    assert isinstance(result, list)
