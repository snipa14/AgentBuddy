# Phase 3 Verification + Phase 21 Security Hardening Pass Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run Phase 3's integration suite for real against a live Docker daemon for the first time, fix the two bugs that surface, then complete Phase 21's hardening pass (deliberately try to break the security boundary) and record findings.

**Architecture:** No new services or public interfaces. All changes are inside `services/executor/` (tests + one small `containers.py`/`config.py` addition for a PID-count cap) plus doc updates (`TODO.md`, `SECURITY.md`). Every new/fixed test runs against a real Docker daemon reachable via a socket bind-mounted into this session's own container (Docker-outside-of-Docker) -- never a mocked `docker.from_env()`, per `.claude/rules/security.md`.

**Tech Stack:** Python 3.12, pytest + pytest-cov, Docker SDK for Python, Pydantic v2, ruff.

**Spec:** No separate spec doc -- this plan implements `TODO.md`'s Phase 3 "Definition of Done" follow-up and Phase 21 ("Security Hardening Pass") verbatim; those checklists are the spec.

**Note on plan location:** This file lives under `services/executor/docs/plans/` rather than the skill's default `docs/superpowers/plans/` because `.claude/hooks/guard_executor_imports.py` scans any file outside `services/executor/` for Docker-SDK-shaped text (`import docker`, `DockerClient(`, `docker.from_env(`, etc.) and blocks the write -- and this plan's task bodies legitimately quote that exact code verbatim as instructions. Placing it under `services/executor/` is also just accurate: every file this plan touches lives there.

## Global Constraints

- Never widen `docker-socket-proxy`'s permission whitelist (`CONTAINERS=1 EXEC=1 IMAGES=1 POST=1`, everything else `0`) -- `.claude/rules/security.md`.
- Every workspace container the Executor creates must keep: non-root UID, `cap_drop=["ALL"]`, `security_opt=["no-new-privileges:true"]`, project-scoped bind mount only, isolated bridge network, `mem_limit`/`nano_cpus` set, guaranteed removal -- `.claude/rules/security.md`.
- No bare `except`; catch a specific exception type or `Exception` with a log -- `CLAUDE.md`.
- Pydantic for all external/config data; type hints everywhere (no untyped `Any`) -- `CLAUDE.md`.
- No inline comments except a single-line `#` for a non-obvious workaround with a GitHub issue reference (this repo has no issue tracker wired up, so avoid new inline comments entirely; put rationale in docstrings) -- `CLAUDE.md`.
- Google-style docstrings on every new public function/class -- `CLAUDE.md`.
- Test naming: `test_<thing_under_test>_<condition>_<expected_outcome>` -- `.claude/rules/testing.md`.
- Integration tests live in `services/executor/tests/integration/`, marked `@pytest.mark.integration` (this file already sets `pytestmark = pytest.mark.integration` at module level) -- `.claude/rules/testing.md`.
- Do not remove or weaken any existing test in `test_executor_sandbox.py` -- `.claude/rules/security.md` ("The mock that proves nothing" / escape-testing rule applies symmetrically: don't delete a real assertion to make a run green).
- Commit directly to `release/v1`, no feature branch or worktree (confirmed project convention -- every prior phase, 0 through 24, committed straight to `release/v1`).
- Only stage files this plan actually touches. The working tree currently has unrelated uncommitted changes (`services/orchestrator/graph/checkpointer.py` and several orchestrator test files, plus an untracked `Hello-World/` dir and `services/orchestrator/conftest.py`) that are **not part of this work** -- never `git add -A`, never touch those files, never stash or discard them.
- Run all `git` commands in this session with `-c safe.directory=/workspace` (this session runs as container root over a repo owned by another UID, which trips git's dubious-ownership guard; this is a per-invocation flag, not a persisted config change -- `git config --global` must never be run per the standing "never update git config" rule).
- Never write a real-looking credential pattern into any file, including test fixtures -- this repo's `block_secret_writes.py` hook scans every write for credential-shaped strings (AWS keys, tokens, private-key blocks) and blocks the write outright even for an obviously-fake test value. Task 7's fake secret literal must be a string with no recognizable credential shape (no `AKIA`-style prefixes, no base64-looking blocks, no `-----BEGIN`).

## Background: two real bugs found running Phase 3's suite live

The first live run (8 tests, real Docker daemon reachable via a bind-mounted host socket) produced **4 passed, 3 failed, 1 skipped**:

1. `test_run_command_cannot_read_a_sibling_projects_files` -- **passed**
2. `test_run_command_cannot_reach_the_docker_socket` -- **passed**
3. `test_run_command_is_capped_by_the_configured_memory_limit` -- **passed**
4. `test_command_exceeding_timeout_raises_and_leaves_no_orphaned_container` -- **passed**
5. `test_go_project_installs_dependency_and_builds` -- **skipped** (`foundry/workspace-go:1.22` not built; that image is Phase 22's deliverable, already tagged `phase-22-done` -- out of scope here, this skip is expected and correct)
6. `test_write_file_round_trip_is_visible_on_the_host` -- **failed**: `FileNotFoundError`, host file never appears
7. `test_read_file_returns_content_written_on_the_host` -- **failed**: same root cause
8. `test_node_stack_run_command_installs_dependencies_from_package_json` -- **failed**: node executable not found

**Bug A (Task 1) -- wrong image used for the node-stack test.** `executor_config`'s fixture builds `ExecutorConfig(...)` directly without a stack registry, leaving `stacks` empty. `image_for_stack("node", config)` (`services/executor/config.py:122`) then falls back to `config.workspace_image` -- the **python** image -- because there's no `"node"` entry to find. The node binary genuinely isn't in that image, hence the exec failure. Production code is unaffected: `ExecutorConfig`'s environment-loading classmethod always loads the real `config/stacks.yaml`, which already has a complete `node` entry. This is a test-fixture bug only.

**Bug B (Task 2) -- Docker-outside-of-Docker (DooD) path mismatch, test-harness only.** This pytest run executes inside a container (this session's own Claude Code container) that reaches the **host's** Docker daemon over a bind-mounted socket. `SECURITY.md`'s existing "The Docker-outside-of-Docker (DooD) detail" section documents exactly this shape of problem for the Executor's own production deployment and how it's solved there (`PROJECTS_HOST_PATH`, an operator-set absolute host path). The test fixture doesn't have an equivalent: `tmp_path_factory.mktemp("projects")` creates a directory inside *this* container's private filesystem, invisible to the host daemon, so when the SDK asks the daemon to bind-mount that path into a sibling workspace container, the daemon mounts nothing/empty at the host end. Both the write and the read test fail because the file each expects never round-trips to a place the daemon can see.

The fix mirrors the production pattern: this container's `/workspace` is bind-mounted from the host at `/home/snipa14/Development/AgentBuddy` (confirmed by the user running this session). A test-only env var (read only by the test file, never by production code) lets the fixture translate a repo-relative tmp dir into both a container-visible path (for the test's own direct file I/O) and the host-visible equivalent (for `ExecutorConfig.projects_host_path`, which the Docker daemon actually uses).

---

## Task 1: Fix the node-stack test fixture to use the real stack registry

**Files:**
- Modify: `services/executor/tests/integration/test_executor_sandbox.py:20` (imports), `:62-70` (`executor_config` fixture)

**Interfaces:**
- Consumes: `services.executor.stacks.load_stacks_config(path: Path) -> StacksConfig` (already exists, `services/executor/stacks.py:111`); `services.executor.config.DEFAULT_STACKS_CONFIG_PATH = "config/stacks.yaml"` (already exists, `services/executor/config.py:20`).
- Produces: nothing new consumed by later tasks -- this is a self-contained fixture fix.

- [ ] **Step 1: Confirm the failing behavior is reproducible in isolation**

Run: `pytest services/executor/tests/integration/test_executor_sandbox.py::test_node_stack_run_command_installs_dependencies_from_package_json -v`
Expected: FAIL (`assert 127 == 0`, node executable not found)

- [ ] **Step 2: Fix the fixture**

In `services/executor/tests/integration/test_executor_sandbox.py`, add to the imports at the top (near the existing `from services.executor.config import ExecutorConfig` line):

```python
from pathlib import Path

from services.executor.config import DEFAULT_STACKS_CONFIG_PATH, ExecutorConfig
from services.executor.stacks import load_stacks_config
```

Replace the `executor_config` fixture body:

```python
@pytest.fixture
def executor_config(tmp_path_factory, workspace_image, workspace_network):
    projects_root = tmp_path_factory.mktemp("projects")
    return ExecutorConfig(
        projects_host_path=projects_root,
        workspace_image=workspace_image,
        network_name=workspace_network,
        command_timeout_seconds=30,
        stacks=load_stacks_config(Path(DEFAULT_STACKS_CONFIG_PATH)),
    )
```

This loads the exact same `config/stacks.yaml` that production loads, so `image_for_stack("node", config)` now correctly resolves to `foundry/workspace-node:20`, and the node stack's registered `manifests`/`install_command`/`cache_dir` entries are present, so the dependency-install step actually runs `npm install` before the test's command executes.

- [ ] **Step 3: Run the node test again**

Run: `pytest services/executor/tests/integration/test_executor_sandbox.py::test_node_stack_run_command_installs_dependencies_from_package_json -v --timeout=180`
Expected: PASS (`result.exit_code == 0` and `"007" in result.stdout`). This will take longer than before (npm install actually runs against the real npm registry) -- allow up to 3 minutes.

- [ ] **Step 4: Run the rest of the suite to confirm nothing else regressed from this fixture change**

Run: `pytest services/executor/tests/integration/test_executor_sandbox.py -v -k "not write_file_round_trip and not read_file_returns_content_written"` (the two DooD-affected tests are excluded here; Task 2 fixes those)
Expected: All remaining tests still PASS.

- [ ] **Step 5: Commit**

```bash
git -c safe.directory=/workspace add services/executor/tests/integration/test_executor_sandbox.py
git -c safe.directory=/workspace commit -m "$(cat <<'EOF'
fix(executor): load real stack registry in sandbox integration fixture

executor_config built ExecutorConfig with an empty stacks registry, so
image_for_stack("node", ...) silently fell back to the python image and
the node-stack test executed against a container with no node binary.
Load config/stacks.yaml the same way production startup does.
EOF
)"
```

---

## Task 2: Fix the DooD host/container path split for the write/read round-trip tests

**Files:**
- Modify: `services/executor/tests/integration/test_executor_sandbox.py` (new helper + fixture changes + 3 test bodies)
- Modify: `.gitignore` (ignore the new local scratch dir)

**Interfaces:**
- Consumes: nothing from Task 1 beyond the already-modified `executor_config` fixture.
- Produces: a `project_roots` fixture with `.local: Path` (this test process's own filesystem view) and `.host: Path` (what the Docker daemon resolves bind-mount sources against) -- used by `project_id`, `node_project_id`, and the three tests below that currently read `executor_config.projects_host_path` directly.

- [ ] **Step 1: Confirm the failing behavior is reproducible in isolation**

Run: `pytest services/executor/tests/integration/test_executor_sandbox.py::test_write_file_round_trip_is_visible_on_the_host -v`
Expected: FAIL with a `FileNotFoundError` on the host-side assertion path.

- [ ] **Step 2: Add the path-translation helper and `project_roots` fixture**

Add near the top of `services/executor/tests/integration/test_executor_sandbox.py`, after the existing module-level constants (`TEST_NETWORK_NAME = ...`):

```python
import os
import shutil
from pathlib import Path
from typing import NamedTuple

REPO_ROOT = Path(__file__).resolve().parents[4]
HOST_WORKSPACE_ROOT = os.environ.get("FOUNDRY_TEST_HOST_WORKSPACE_ROOT")
_DOOD_LOCAL_BASE = REPO_ROOT / ".pytest-dood-projects"


class ProjectRoots(NamedTuple):
    """Where this test process should read/write vs. where Docker should bind-mount from.

    Attributes:
        local: The path this pytest process can open()/mkdir() directly.
        host: The path to hand to ExecutorConfig.projects_host_path -- what
            the Docker daemon actually resolves bind-mount sources against.
    """

    local: Path
    host: Path


def _make_project_roots(tmp_path_factory) -> ProjectRoots:
    """Builds a fresh projects-root pair for one test.

    When FOUNDRY_TEST_HOST_WORKSPACE_ROOT is unset, this test process and
    the Docker daemon share one filesystem (the common case: pytest running
    directly on the same host as the daemon), so an ordinary pytest tmp dir
    works as both paths.

    When it is set, this test process runs inside a container that reaches
    a *host* Docker daemon over a bind-mounted socket (Docker-outside-of-
    Docker -- see SECURITY.md's "DooD detail", which documents the same
    shape of problem for the Executor's own production deployment and
    solves it with PROJECTS_HOST_PATH). FOUNDRY_TEST_HOST_WORKSPACE_ROOT is
    that same idea applied to this test process: the host-side absolute
    path this container's repo checkout is bind-mounted from. A tmp dir is
    created under the repo root (so it's covered by that same bind mount)
    and its host-side equivalent is derived by substituting the repo root
    prefix.

    Args:
        tmp_path_factory: pytest's tmp_path_factory fixture.

    Returns:
        A ProjectRoots pair.
    """
    if not HOST_WORKSPACE_ROOT:
        local = tmp_path_factory.mktemp("projects")
        return ProjectRoots(local=local, host=local)
    local = _DOOD_LOCAL_BASE / uuid.uuid4().hex[:8]
    local.mkdir(parents=True)
    host = Path(HOST_WORKSPACE_ROOT) / local.relative_to(REPO_ROOT)
    return ProjectRoots(local=local, host=host)


@pytest.fixture(scope="session", autouse=True)
def _cleanup_dood_local_projects_dir():
    yield
    if HOST_WORKSPACE_ROOT:
        shutil.rmtree(_DOOD_LOCAL_BASE, ignore_errors=True)


@pytest.fixture
def project_roots(tmp_path_factory):
    return _make_project_roots(tmp_path_factory)
```

(`uuid` is already imported at the top of the file.)

- [ ] **Step 3: Wire `project_roots` into the existing fixtures**

Replace the `executor_config`, `project_id`, and `node_project_id` fixtures:

```python
@pytest.fixture
def executor_config(project_roots, workspace_image, workspace_network):
    return ExecutorConfig(
        projects_host_path=project_roots.host,
        workspace_image=workspace_image,
        network_name=workspace_network,
        command_timeout_seconds=30,
        stacks=load_stacks_config(Path(DEFAULT_STACKS_CONFIG_PATH)),
    )


@pytest.fixture
def project_id(project_roots):
    new_project_id = f"proj-{uuid.uuid4().hex[:8]}"
    (project_roots.local / new_project_id).mkdir()
    return new_project_id


@pytest.fixture
def node_project_id(project_roots, node_workspace_image):
    new_project_id = f"proj-{uuid.uuid4().hex[:8]}"
    project_dir = project_roots.local / new_project_id
    project_dir.mkdir()
    (project_dir / "package.json").write_text(
        '{"name": "sandbox-check", "version": "1.0.0",'
        ' "dependencies": {"left-pad": "1.3.0"}}'
    )
    return new_project_id
```

- [ ] **Step 4: Update the three test bodies that build a host-side path directly**

`test_write_file_round_trip_is_visible_on_the_host`:

```python
def test_write_file_round_trip_is_visible_on_the_host(
    docker_client, executor_config, project_roots, project_id
):
    tool_write_file(docker_client, executor_config, project_id, "hello.txt", "hi from the sandbox")

    host_file = project_roots.local / project_id / "hello.txt"
    assert host_file.read_text() == "hi from the sandbox"
```

`test_read_file_returns_content_written_on_the_host`:

```python
def test_read_file_returns_content_written_on_the_host(
    docker_client, executor_config, project_roots, project_id
):
    host_file = project_roots.local / project_id / "existing.txt"
    host_file.write_text("already here")

    content = tool_read_file(docker_client, executor_config, project_id, "existing.txt")

    assert content == "already here"
```

`test_run_command_cannot_read_a_sibling_projects_files`:

```python
def test_run_command_cannot_read_a_sibling_projects_files(
    docker_client, executor_config, project_roots, project_id
):
    other_project_id = f"proj-{uuid.uuid4().hex[:8]}"
    other_dir = project_roots.local / other_project_id
    other_dir.mkdir()
    (other_dir / "secret.txt").write_text("sibling project secret")

    result = tool_run_command(
        docker_client,
        executor_config,
        project_id,
        ["cat", f"/workspace/../{other_project_id}/secret.txt"],
    )

    assert "sibling project secret" not in result.stdout
    assert result.exit_code != 0
```

- [ ] **Step 5: Add the gitignore entry**

Add to `.gitignore`, near the existing `.pytest_cache/` line:

```
.pytest-dood-projects/
```

- [ ] **Step 6: Run with the real host path set**

Run: `FOUNDRY_TEST_HOST_WORKSPACE_ROOT=/home/snipa14/Development/AgentBuddy pytest services/executor/tests/integration/test_executor_sandbox.py -v -k "round_trip or read_file_returns_content_written or sibling_projects"`
Expected: All three PASS for real (no `FileNotFoundError`).

- [ ] **Step 7: Run the full sandbox file once more, with the env var set, to confirm no regression**

Run: `FOUNDRY_TEST_HOST_WORKSPACE_ROOT=/home/snipa14/Development/AgentBuddy pytest services/executor/tests/integration/test_executor_sandbox.py -v`
Expected: 7 PASS (all of `test_executor_sandbox.py`'s tests at this point; the go-stack test lives in a separate file).

- [ ] **Step 8: Commit**

```bash
git -c safe.directory=/workspace add services/executor/tests/integration/test_executor_sandbox.py .gitignore
git -c safe.directory=/workspace commit -m "$(cat <<'EOF'
fix(executor): translate host/container paths in DooD sandbox tests

tmp_path_factory dirs live inside this test process's own container
filesystem, invisible to a host Docker daemon reached over a bind-mounted
socket -- the same DooD shape SECURITY.md already documents for the
Executor's own deployment. FOUNDRY_TEST_HOST_WORKSPACE_ROOT lets the
fixture translate a repo-relative tmp dir into both the container-local
path this test process uses directly and the host-visible path Docker
actually bind-mounts from. Unset (the common non-nested case), behavior
is unchanged.
EOF
)"
```

---

## Task 3: Confirm Phase 3 is fully green and update its verification status

**Files:**
- Modify: `TODO.md` (Phase 3's "Verification status" line, ~line 84)

**Interfaces:**
- Consumes: Tasks 1 and 2's fixes.
- Produces: nothing consumed by later tasks.

- [ ] **Step 1: Run the full executor test suite (unit + integration) once, clean**

Run: `pytest services/executor/tests/unit -v` then `FOUNDRY_TEST_HOST_WORKSPACE_ROOT=/home/snipa14/Development/AgentBuddy pytest -m integration services/executor/tests/integration -v`
Expected: unit suite unchanged (115 passed, as already confirmed earlier this session); integration suite now 7 passed, 1 skipped (go image), 0 failed.

- [ ] **Step 2: Update TODO.md's Phase 3 verification status**

Replace the existing "**Verification status:**" paragraph under "## Phase 3 -- Executor & Docker Security Boundary" (`TODO.md` around line 84) with:

```markdown
**Verification status:** Fully verified against a real Docker daemon. `services/executor`'s unit suite is green (115 tests). The real-Docker integration suite (`pytest -m integration services/executor/tests/integration`) ran against a live daemon (Docker-outside-of-Docker: this session's own container reaches the host daemon over a bind-mounted socket) and is green for real: 7 passed, 1 skipped (`foundry/workspace-go:1.22` isn't built on this host -- that image is Phase 22's deliverable, already tagged `phase-22-done`, and out of Phase 3's scope). Two bugs surfaced and were fixed on this pass, both in test infrastructure, not Executor logic: the node-stack test's fixture built an `ExecutorConfig` with an empty stack registry (silently falling back to the python image instead of the node one -- fixed by loading `config/stacks.yaml` the same way production startup does), and the write/read round-trip tests assumed pytest and the Docker daemon share one filesystem, which doesn't hold when the test runner itself is a container reaching a host daemon over a mounted socket (fixed with a `FOUNDRY_TEST_HOST_WORKSPACE_ROOT`-driven local/host path split, see `test_executor_sandbox.py`). Every containment claim in `services/executor/SECURITY.md`'s threat-model table is now backed by a passing, real-daemon test.
```

- [ ] **Step 3: Commit**

```bash
git -c safe.directory=/workspace add TODO.md
git -c safe.directory=/workspace commit -m "$(cat <<'EOF'
docs(phase-3): record real green run of the executor integration suite

First live-daemon run of services/executor/tests/integration since Phase
3 landed. Two test-infrastructure bugs found and fixed (see prior two
commits); all containment claims are now backed by a passing real-Docker
test, closing Phase 3's previously-open verification gap.
EOF
)"
```

No new tag here -- `phase-3-done` already exists from Phase 3's original landing; this closes out a verification gap on an already-tagged phase, it doesn't re-end it.

---

## Task 4: Add a PID-count cap to workspace containers (Phase 21 finding)

**Files:**
- Modify: `services/executor/config.py` (`ExecutorConfig` + its environment-loading classmethod)
- Modify: `services/executor/containers.py` (`workspace_container`)
- Modify: `services/executor/tests/unit/test_config.py` (new default/env-var test)
- Modify: `services/executor/tests/unit/test_containers_mocked.py` (assert the new kwarg reaches the container-creation call)

**Interfaces:**
- Consumes: `ExecutorConfig` (existing).
- Produces: `ExecutorConfig.pids_limit: int` (new field, default `256`), read via `WORKSPACE_PIDS_LIMIT` in the environment-loading classmethod; passed as `pids_limit=config.pids_limit` into the container-creation call in `workspace_container()`. Task 6 (fork-bomb test) depends on this field existing and being enforced.

- [ ] **Step 1: Confirm the gap**

Run: `grep -n "pids_limit" services/executor/containers.py services/executor/config.py`
Expected: no matches. Every other resource cap (`mem_limit`, `nano_cpus`, `cap_drop`, `security_opt`) is set; process-count is not. A fork bomb inside a workspace container is capped by memory/CPU but not by process count, meaning it can still exhaust the **host's** total PID table (a resource shared across every container on the host, not scoped per-container) before memory or CPU limits have any effect.

- [ ] **Step 2: Write the failing unit test for the config default**

Add to `services/executor/tests/unit/test_config.py`:

```python
def test_executor_config_pids_limit_defaults_to_256():
    config = ExecutorConfig(projects_host_path=Path("/tmp/whatever"))
    assert config.pids_limit == 256


def test_from_env_reads_workspace_pids_limit(monkeypatch, tmp_path):
    stacks_path = tmp_path / "stacks.yaml"
    stacks_path.write_text("stacks: {}\n")
    monkeypatch.setenv("PROJECTS_HOST_PATH", "/tmp/whatever")
    monkeypatch.setenv("STACKS_CONFIG_PATH", str(stacks_path))
    monkeypatch.setenv("WORKSPACE_PIDS_LIMIT", "64")

    config = ExecutorConfig.from_env()

    assert config.pids_limit == 64
```

(Match whatever `Path`/`monkeypatch` imports and existing environment-loading test patterns this file already uses -- check the top of `test_config.py` for its existing `PROJECTS_HOST_PATH`/`STACKS_CONFIG_PATH` env-setup convention before adding these, and reuse it rather than inventing a second pattern.)

- [ ] **Step 3: Run to verify it fails**

Run: `pytest services/executor/tests/unit/test_config.py -v -k pids_limit`
Expected: FAIL with an `AttributeError` or a Pydantic validation error (`pids_limit` doesn't exist yet).

- [ ] **Step 4: Add the field**

In `services/executor/config.py`, add near the other `DEFAULT_*` constants:

```python
DEFAULT_PIDS_LIMIT = 256
```

Add to `ExecutorConfig`, alongside `nano_cpus`:

```python
pids_limit: int = DEFAULT_PIDS_LIMIT
```

Add to the environment-loading classmethod's returned `cls(...)` call, alongside `nano_cpus=...`:

```python
pids_limit=int(_env_or_default("WORKSPACE_PIDS_LIMIT", str(DEFAULT_PIDS_LIMIT))),
```

Update the `ExecutorConfig` docstring's second sentence (currently naming `mem_limit`/`nano_cpus` as the process-wide v1 defaults) to also name `pids_limit`.

- [ ] **Step 5: Run to verify the config tests pass**

Run: `pytest services/executor/tests/unit/test_config.py -v`
Expected: all PASS, including the two new tests.

- [ ] **Step 6: Wire it into container creation**

In `services/executor/containers.py`, find the container-creation call inside `workspace_container()` (the one already passing `cap_drop=["ALL"]`, `security_opt=["no-new-privileges:true"]`, `mem_limit=config.mem_limit`, `nano_cpus=config.nano_cpus`). Add `pids_limit=config.pids_limit` to that same call.

- [ ] **Step 7: Update the mocked unit test**

In `services/executor/tests/unit/test_containers_mocked.py`, find the test that asserts the container-creation call's kwargs (it very likely already asserts on `mem_limit=`/`nano_cpus=`/`cap_drop=` -- check the existing assertion, whatever it's named). Add an assertion that `pids_limit=config.pids_limit` (or the equivalent concrete literal that test already uses for `nano_cpus`) is present in the same call.

- [ ] **Step 8: Run the full mocked unit suite**

Run: `pytest services/executor/tests/unit -v`
Expected: all PASS (115 + 2 new from Step 5 = 117; the containers-mocked test from Step 7 updates an existing test rather than adding one).

- [ ] **Step 9: Commit**

```bash
git -c safe.directory=/workspace add services/executor/config.py services/executor/containers.py services/executor/tests/unit/test_config.py services/executor/tests/unit/test_containers_mocked.py
git -c safe.directory=/workspace commit -m "$(cat <<'EOF'
fix(executor): cap workspace container process count (Phase 21 finding)

Every other per-container resource (memory, CPU, capabilities) was
capped except process count. A fork bomb inside a workspace container
was bounded by mem_limit/nano_cpus but could still exhaust the host's
shared PID table before either limit engaged. Adds pids_limit
(default 256, WORKSPACE_PIDS_LIMIT-configurable) alongside the existing
caps.
EOF
)"
```

---

## Task 5: Fork-bomb / resource-exhaustion integration test

**Files:**
- Modify: `services/executor/tests/integration/test_executor_sandbox.py` (one new test)

**Interfaces:**
- Consumes: Task 4's `pids_limit` enforcement.
- Produces: nothing consumed by later tasks.

- [ ] **Step 1: Write the test**

Add to `services/executor/tests/integration/test_executor_sandbox.py`:

```python
def test_run_command_fork_bomb_is_capped_and_leaves_no_orphaned_container(
    docker_client, executor_config, project_id
):
    result = tool_run_command(
        docker_client,
        executor_config,
        project_id,
        ["bash", "-c", ":(){ :|:& };:"],
        timeout=15,
    )

    assert result.exit_code != 0

    leftover = docker_client.containers.list(
        all=True, filters={"ancestor": executor_config.workspace_image}
    )
    assert leftover == []
```

This proves two things at once: (1) the container doesn't survive/succeed running an unbounded fork bomb (it's killed by the `pids_limit` cap or the command timeout, either way `exit_code != 0`), and (2) the existing cleanup guarantee (`workspace_container`'s `finally`-block removal, already proven by `test_command_exceeding_timeout_raises_and_leaves_no_orphaned_container`) holds even under this specific abuse pattern, not just a plain `sleep`.

- [ ] **Step 2: Run it**

Run: `FOUNDRY_TEST_HOST_WORKSPACE_ROOT=/home/snipa14/Development/AgentBuddy pytest services/executor/tests/integration/test_executor_sandbox.py::test_run_command_fork_bomb_is_capped_and_leaves_no_orphaned_container -v`
Expected: PASS. If it hangs or times out at the pytest level rather than the container level, that itself is a finding to write up in Task 8 (it would mean `pids_limit` isn't actually stopping the bomb quickly and the 15s command-timeout is doing all the work) -- capture whichever behavior actually happens, don't assume.

- [ ] **Step 3: Run the whole sandbox file once more**

Run: `FOUNDRY_TEST_HOST_WORKSPACE_ROOT=/home/snipa14/Development/AgentBuddy pytest services/executor/tests/integration/test_executor_sandbox.py -v`
Expected: all 8 tests now in this file PASS.

- [ ] **Step 4: Commit**

```bash
git -c safe.directory=/workspace add services/executor/tests/integration/test_executor_sandbox.py
git -c safe.directory=/workspace commit -m "$(cat <<'EOF'
test(executor): prove a fork bomb is capped and cleaned up (Phase 21)

Exercises the pids_limit cap added in the previous commit against a real
fork bomb, and confirms the existing container-cleanup guarantee holds
under this abuse pattern too, not just a plain timeout.
EOF
)"
```

---

## Task 6: Proxy-boundary integration test (denied vs. allowed endpoints)

**Files:**
- Create: `services/executor/tests/integration/test_proxy_boundary.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: nothing consumed by later tasks.

**Context:** `docker-compose.yml` already defines the real `docker-socket-proxy` service with the production whitelist (`CONTAINERS=1 EXEC=1 IMAGES=1 POST=1`, everything else `0`). This test starts *only* that one service, joins this test-runner's own container to its network so the proxy's hostname resolves, hits one denied endpoint and one allowed endpoint through it, and asserts the whitelist actually holds both directions (not just "everything happens to fail").

- [ ] **Step 1: Write the test file**

```python
"""Integration test proving docker-socket-proxy's whitelist actually holds.

Requires a real Docker daemon and docker compose (docker-socket-proxy's
production config lives in docker-compose.yml -- this starts just that one
service, never the full stack, so it needs no secrets/.env). Skipped
cleanly if compose or the daemon aren't available.

Run with: pytest -m integration services/executor/tests/integration/test_proxy_boundary.py
"""

import os
import subprocess

import pytest
from docker import errors as docker_errors
from docker.client import DockerClient

from services.executor.docker_client import build_docker_client

PROXY_CONTAINER_NAME = "foundry-docker-socket-proxy"
PROXY_NETWORK_NAME = "foundry_internal"
PROXY_PORT = 2375

pytestmark = pytest.mark.integration


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
    up = subprocess.run(
        ["docker", "compose", "up", "-d", "docker-socket-proxy"],
        cwd="/workspace",
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


@pytest.fixture(scope="module")
def proxy_reachable_from_this_process(running_proxy):
    self_id = os.environ.get("HOSTNAME")
    if not self_id:
        pytest.skip("HOSTNAME env var not set -- can't identify this container to network-connect it")
    connect = subprocess.run(
        ["docker", "network", "connect", PROXY_NETWORK_NAME, self_id],
        capture_output=True,
        text=True,
    )
    if connect.returncode != 0 and "already exists" not in connect.stderr:
        pytest.skip(f"could not join {PROXY_NETWORK_NAME}: {connect.stderr}")
    yield
    subprocess.run(
        ["docker", "network", "disconnect", PROXY_NETWORK_NAME, self_id],
        capture_output=True,
        text=True,
    )


@pytest.fixture
def proxy_client(proxy_reachable_from_this_process):
    client = DockerClient(base_url=f"tcp://docker-socket-proxy:{PROXY_PORT}")
    try:
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
```

Note: `images.build` needs the request to actually reach the proxy's `BUILD`-gated check; if this particular assertion turns out to fail because the proxy responds with a different denial status than 403 (e.g. a 400 for a malformed request shape before the whitelist check even runs), adjust the assertion to whatever status code the proxy actually returns for a denied build, but keep the assertion that it is **not** 200/201 (i.e. the build never actually happens) as the non-negotiable part of this test.

- [ ] **Step 2: Run it**

Run: `pytest -m integration services/executor/tests/integration/test_proxy_boundary.py -v`
Expected: PASS, or a clean, informative `SKIPPED` (with reason) if this specific sandbox's networking doesn't allow a network-connect from inside this nested container -- capture whichever outcome actually happens, this is exactly the kind of thing Task 7 documents either way. If it skips, do not spend more than one extra retry attempting to force it through; document the skip reason in `SECURITY.md` instead (Task 8) as an accepted verification gap, the same way Phase 3's original doc handled the "no Docker daemon in this sandbox" gap.

- [ ] **Step 3: Commit**

```bash
git -c safe.directory=/workspace add services/executor/tests/integration/test_proxy_boundary.py
git -c safe.directory=/workspace commit -m "$(cat <<'EOF'
test(executor): prove docker-socket-proxy's whitelist holds (Phase 21)

Starts the real docker-socket-proxy service from docker-compose.yml and
hits it directly (not the raw socket) to confirm BUILD/NETWORKS stay
denied (403) while the whitelisted CONTAINERS endpoint still works --
proving the proxy config isn't just narrow on paper.
EOF
)"
```

---

## Task 7: Secret-redaction-in-logs integration test

**Files:**
- Modify: `services/executor/tests/integration/test_executor_sandbox.py` (one new test)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: nothing consumed by later tasks.

- [ ] **Step 1: Write the test**

Add to `services/executor/tests/integration/test_executor_sandbox.py`:

```python
def test_run_command_never_logs_literal_file_contents(
    docker_client, executor_config, project_roots, project_id, caplog
):
    fake_secret_value = "totally-not-a-real-credential-value-12345"
    secret_file = project_roots.local / project_id / "secret.env"
    secret_file.write_text(f"API_KEY={fake_secret_value}\n")

    with caplog.at_level("DEBUG"):
        tool_run_command(docker_client, executor_config, project_id, ["cat", "secret.env"])

    for record in caplog.records:
        assert fake_secret_value not in record.getMessage()
```

Use exactly this string (or another value with no recognizable credential shape -- no `AKIA`-style prefix, no base64-looking block, no `-----BEGIN` header). This repo's `block_secret_writes.py` hook scans file writes for credential-shaped patterns and will block the write if the fake value looks too much like a real key.

This proves the Executor's own logging (already-read calls in `tools.py` like `logger.info("project %s: run_command(%s)", project_id, command)`) never includes file contents -- only `project_id` and the command argv, which is what `SECURITY.md`'s existing table already claims. The command's *output* (`result.stdout`, containing the fake secret) is returned to the caller normally -- that's expected, correct behavior (the Coder agent needs to read the file it just asked to read) -- this test is specifically about the Executor's own log stream, not about redacting tool results.

- [ ] **Step 2: Run it**

Run: `FOUNDRY_TEST_HOST_WORKSPACE_ROOT=/home/snipa14/Development/AgentBuddy pytest services/executor/tests/integration/test_executor_sandbox.py::test_run_command_never_logs_literal_file_contents -v`
Expected: PASS. If it fails, that's a real finding (some log call is including command output/content) -- fix the offending logging call in `services/executor/tools.py` to log only `project_id`/`path`/`command`, never content or `result.stdout`, then re-run.

- [ ] **Step 3: Commit**

```bash
git -c safe.directory=/workspace add services/executor/tests/integration/test_executor_sandbox.py
git -c safe.directory=/workspace commit -m "$(cat <<'EOF'
test(executor): prove file contents never reach the Executor's own logs

Phase 21's "grep logs for literal secrets" check, made permanent as a
regression test rather than a one-time manual grep.
EOF
)"
```

---

## Task 8: Write Phase 21 findings into SECURITY.md and close out TODO.md

**Files:**
- Modify: `services/executor/SECURITY.md` (new "Phase 21 hardening pass findings" section)
- Modify: `TODO.md` (check off Phase 21's five boxes, add its own Verification status line)

**Interfaces:**
- Consumes: the actual pass/fail/skip outcomes from Tasks 5, 6, 7 (this task's writer must know what really happened, not assume everything passed).

- [ ] **Step 1: Add a findings section to SECURITY.md**

Insert a new section after "## Escape testing" in `services/executor/SECURITY.md`:

```markdown
## Phase 21 hardening pass findings

Deliberate attempts to break every boundary claim above, against a real
Docker daemon:

- **Path traversal out of the bind mount** -- already covered before this
  pass, at both layers: `services/executor/tests/unit/test_paths.py`
  (`resolve_project_path` rejects `..` segments and absolute-path escapes
  before any Docker call happens) and
  `test_run_command_cannot_read_a_sibling_projects_files` (a live
  container genuinely cannot see a sibling project's directory, because
  it was never mounted). Re-run and confirmed still green on this pass.
- **Reaching docker.sock or a denied proxy endpoint directly** --
  `test_run_command_cannot_reach_the_docker_socket` (already existing)
  confirms a workspace container has no access to the raw socket.
  `test_proxy_boundary.py` (new this pass) goes further and hits the real
  docker-socket-proxy service directly: [FILL IN AT EXECUTION TIME --
  either "confirmed BUILD and NETWORKS both return 403 while the
  whitelisted CONTAINERS endpoint still works" if Task 6 passed, or the
  exact skip reason if this sandbox's networking couldn't support the
  network-connect step, e.g. "skipped in this sandbox: <reason> -- an
  accepted verification gap for this run, same shape as Phase 3's
  original no-daemon gap, re-run on a host without this constraint before
  treating the proxy config itself as independently proven"].
- **Fork bomb / resource exhaustion** -- found a real gap: every other
  per-container resource (memory via `mem_limit`, CPU via `nano_cpus`,
  capabilities via `cap_drop`) was capped, but process count was not. A
  fork bomb was bounded by memory/CPU limits but could still exhaust the
  **host's** shared PID table before either engaged. Fixed by adding
  `pids_limit` (default 256, `WORKSPACE_PIDS_LIMIT`-configurable) to every
  workspace container alongside the existing caps.
  `test_run_command_fork_bomb_is_capped_and_leaves_no_orphaned_container`
  proves the fix and that container cleanup still holds under this
  specific abuse pattern.
- **Secrets in logs** -- audited every logging call in
  `services/executor/tools.py`: all of them log `project_id`,
  `path`/`command`, or an exception's type -- never file contents or a
  command's captured output.
  `test_run_command_never_logs_literal_file_contents` makes this a
  permanent regression test rather than a one-time grep.
```

Fill in the bracketed placeholder with the actual Task 6 outcome before committing -- do not commit this file with the placeholder text still in it.

- [ ] **Step 2: Check off Phase 21 in TODO.md**

In `TODO.md`, under "## Phase 21 -- Security Hardening Pass", check all five boxes (`- [ ]` to `- [x]`) and replace the current empty space after "**Definition of Done:** ..." with:

```markdown
**Verification status:** All five items attempted against a real Docker daemon (Docker-outside-of-Docker: this session's own container reached the host daemon over a bind-mounted socket). One real gap found and fixed: workspace containers had no process-count cap (`pids_limit`), now added. Full findings and the proxy-boundary test's actual pass/skip outcome are in `services/executor/SECURITY.md`'s "Phase 21 hardening pass findings" section.
```

- [ ] **Step 3: Commit and tag**

```bash
git -c safe.directory=/workspace add services/executor/SECURITY.md TODO.md
git -c safe.directory=/workspace commit -m "$(cat <<'EOF'
feat(phase-21): security hardening pass

Deliberately attempted every boundary break TODO.md's Phase 21 lists
against a real Docker daemon. Found and fixed one real gap (no
per-container PID cap); the other three checks confirmed existing
mitigations hold. Findings recorded in services/executor/SECURITY.md.
EOF
)"
git -c safe.directory=/workspace tag phase-21-done
```

---

## Task 9: Final full-suite verification before considering this done

**Files:** none (verification only)

- [ ] **Step 1: Run the full executor suite one more time, clean, from a fresh shell state**

Run: `pytest services/executor/tests/unit -v` then `FOUNDRY_TEST_HOST_WORKSPACE_ROOT=/home/snipa14/Development/AgentBuddy pytest -m integration services/executor/tests/integration -v`
Expected: unit suite green (117 tests per Task 4's additions); integration suite green (10 tests: 7 original `test_executor_sandbox.py` + fork-bomb test + node fix + go skip, plus whatever `test_proxy_boundary.py` contributes per its actual Task 6 outcome).

- [ ] **Step 2: Run ruff on every file this plan touched**

Run: `ruff check services/executor/ && ruff format --check services/executor/`
Expected: clean. If ruff reports anything on files this plan touched, fix it before considering the task done; do not touch unrelated pre-existing ruff findings elsewhere in the repo (out of scope, and the working tree already has unrelated uncommitted changes per this plan's Global Constraints).

- [ ] **Step 3: Confirm git state matches expectations**

Run: `git -c safe.directory=/workspace log --oneline release/v1 -10` and `git -c safe.directory=/workspace status`
Expected: the ~8 commits from Tasks 1-8 (fixture fixes, pids_limit, three new tests, two doc updates) at the tip of `release/v1`, `phase-21-done` tag present, and `git status` showing only the same pre-existing unrelated orchestrator changes/untracked files that were present before this plan started (nothing from this plan left uncommitted).

- [ ] **Step 4: Report final status to the user**

Summarize: Phase 3's integration suite is now genuinely green against a live daemon (two test-infra bugs fixed). Phase 21 is complete, tagged `phase-21-done`, with one real production fix (`pids_limit`) and its findings recorded in `SECURITY.md`. State plainly whether Task 6's proxy-boundary test passed live or had to document a skip, since that's the one outcome genuinely unknown until execution.

---

## Task 10: Fix workspace cache-volume ownership (new finding surfaced by Task 2 -- execute before Task 3)

**Controller's note (added after Task 2's real run, not part of the original plan draft):** Task 2's DooD path fix correctly resolved package.json's visibility, but `test_node_stack_run_command_installs_dependencies_from_package_json` still failed afterward, now with `npm error code EACCES` on `/home/sandbox/.npm/_cacache`. Root cause, confirmed directly: a brand-new Docker named volume mounted over a path with no pre-existing content in the image is created **root:root** by the daemon regardless of the container's runtime `--user`. `services/executor/containers.py`'s Phase-17 per-project-per-stack cache volume (`foundry-cache-{project_id}-{stack}`) is always freshly created (keyed by a fresh `project_id` every run), and `node:20-slim`'s image never pre-creates `/home/sandbox/.npm`, so the non-root UID-1000 sandbox user gets `EACCES` on first write. This is a real, previously-undiscovered Phase-17 bug -- it was never caught before because this integration suite had never run against a live Docker daemon until this session. This task fixes it at the image-template level: Docker seeds a fresh named volume's initial content (and ownership) from whatever already exists at that path in the image, on first mount -- so pre-creating and chowning each stack's `cache_dir` at build time avoids the root:root default entirely.

**Files:**
- Modify: `images/workspace.template.Dockerfile`
- Modify: `services/executor/imagegen.py`
- Modify: `services/executor/tests/unit/test_imagegen.py`
- Regenerate (via `scripts/gen_workspace_images.py`, not by hand): `images/workspace-python/Dockerfile`, `images/workspace-node/Dockerfile`, `images/workspace-go/Dockerfile` (whichever of these exist as committed files -- check with `ls images/` first; Phase 22 is tagged done so `workspace-go` likely already exists, but confirm rather than assume)

**Interfaces:**
- Consumes: `StackConfig.cache_dir: str | None` (already exists, `services/executor/stacks.py`).
- Produces: nothing new consumed by later tasks -- Task 3 (already in the queue) re-verifies the full suite and will now see the node test pass for real.

- [ ] **Step 1: Confirm the gap directly**

Run: `grep -n "cache_dir\|chown" images/workspace.template.Dockerfile services/executor/imagegen.py`
Expected: `cache_dir` appears nowhere in the template or generator; `chown` appears nowhere either. Every workspace image currently ships its cache directory (when the stack declares one) with no pre-existing content, so the first named-volume mount over it is always root:root.

- [ ] **Step 2: Write the failing unit tests**

Add to `services/executor/tests/unit/test_imagegen.py`:

```python
def test_render_includes_cache_dir_setup_when_stack_declares_one():
    stack = StackConfig(
        image="foundry/workspace-python:3.12",
        base_image="python:3.12-slim",
        apt_packages=["build-essential", "git", "curl"],
        pip_packages=["pytest", "pytest-cov", "playwright", "ruff"],
        cache_dir="/home/sandbox/.cache/pip",
    )
    rendered = render_dockerfile("python", stack, TEMPLATE_PATH.read_text())
    lines = instruction_lines(rendered)
    assert (
        "RUN mkdir -p /home/sandbox/.cache/pip "
        "&& chown 1000:1000 /home/sandbox/.cache/pip"
    ) in lines


def test_render_omits_cache_dir_setup_when_stack_has_no_cache_dir():
    rendered = render_dockerfile("python", _python_stack(), TEMPLATE_PATH.read_text())
    assert "chown 1000:1000" not in rendered
```

(`_python_stack()` already exists in this file and does not set `cache_dir`, so it defaults to `None` -- exactly the case the second test needs.)

- [ ] **Step 3: Run to verify they fail**

Run: `pytest services/executor/tests/unit/test_imagegen.py -v -k cache_dir_setup`
Expected: FAIL (`cache_dir_setup` isn't a valid template placeholder yet / the chown line doesn't render).

- [ ] **Step 4: Add the template placeholder**

In `images/workspace.template.Dockerfile`, change:

```
$sandbox_user_block
WORKDIR /workspace
USER 1000:1000
```

to:

```
$sandbox_user_block
$cache_dir_setup
WORKDIR /workspace
USER 1000:1000
```

- [ ] **Step 5: Add the rendering function and wire it in**

In `services/executor/imagegen.py`, add this function near the top-level (after the module-level `_CREATE_SANDBOX_BLOCK`/`_RENAME_SANDBOX_BLOCK` constants, before `render_dockerfile`):

```python
def _cache_dir_setup(cache_dir: str | None) -> str:
    """Renders the build-time cache-directory ownership fix, or nothing.

    A freshly created Docker named volume mounted over a path with no
    existing content in the image is created root:root by the daemon,
    regardless of the container's runtime --user. Pre-creating the
    directory in the image with the sandbox UID/GID lets Docker seed the
    volume's initial content (and ownership) from the image layer on first
    mount instead, so the non-root sandbox user can write into its
    package-manager cache from the very first container run.

    Args:
        cache_dir: The stack's cache directory, or None if it has no
            per-project cache volume.

    Returns:
        A RUN instruction pre-creating and chowning cache_dir, or "" if
        cache_dir is None.
    """
    if cache_dir is None:
        return ""
    return f"RUN mkdir -p {cache_dir} && chown 1000:1000 {cache_dir}\n"
```

In `render_dockerfile`'s `string.Template(template_text).substitute(...)` call, add one line alongside the existing `sandbox_user_block=sandbox_user_block,` argument:

```python
        cache_dir_setup=_cache_dir_setup(stack.cache_dir),
```

- [ ] **Step 6: Run the new unit tests, then the whole imagegen unit file**

Run: `pytest services/executor/tests/unit/test_imagegen.py -v`
Expected: the two new tests PASS. `test_golden_rendered_matches_committed_dockerfiles` now FAILS (expected at this point -- the committed Dockerfiles haven't been regenerated yet; confirm the failure message is exactly the "out of sync with the template" one this test already raises, not something new).

- [ ] **Step 7: Regenerate the committed Dockerfiles**

Run: `ls images/` first to see which `workspace-*` directories exist, then: `python3 scripts/gen_workspace_images.py`
Expected: it prints one line per stack in `config/stacks.yaml` (`python`, `node`, `go`) with the path it wrote.

- [ ] **Step 8: Confirm the golden test passes now**

Run: `pytest services/executor/tests/unit/test_imagegen.py -v`
Expected: all PASS, including `test_golden_rendered_matches_committed_dockerfiles`.

- [ ] **Step 9: Diff the regenerated Dockerfiles to confirm only the intended change landed**

Run: `git diff images/workspace-python/Dockerfile images/workspace-node/Dockerfile` (and `images/workspace-go/Dockerfile` if it exists)
Expected: each diff shows exactly one new `RUN mkdir -p <cache_dir> && chown 1000:1000 <cache_dir>` line inserted in the expected place (after the sandbox-user block, before `WORKDIR /workspace`), and nothing else changed. If anything else differs (e.g. because the template had drifted from the committed files before this task for an unrelated reason), stop and report DONE_WITH_CONCERNS rather than committing an unexplained diff.

- [ ] **Step 10: Force a rebuild of the live images this session's tests actually use**

The integration test fixtures (`workspace_image`, `node_workspace_image` in `test_executor_sandbox.py`) only build an image if the tag doesn't already exist -- a tag built earlier this session with the old Dockerfile will NOT pick up this fix on its own. Remove the existing tags so the next test run rebuilds them for real:

Run: `docker rmi foundry/workspace-python:3.12 foundry/workspace-node:20`

(Omit `foundry/workspace-go:1.22` -- it was never built in this sandbox, per Phase 3/22's already-documented gap, and stays that way.)

- [ ] **Step 11: Re-run the node-stack test for real**

Run: `FOUNDRY_TEST_HOST_WORKSPACE_ROOT=/home/snipa14/Development/AgentBuddy pytest services/executor/tests/integration/test_executor_sandbox.py::test_node_stack_run_command_installs_dependencies_from_package_json -v --timeout=300`
Expected: PASS. This rebuilds `foundry/workspace-node:20` from scratch (Step 10 removed the tag), so allow extra time. If it still fails, do not guess at a second fix -- report BLOCKED with the actual error, since this task's fix is specifically targeted at the EACCES root cause and a different failure here means the diagnosis needs revisiting.

- [ ] **Step 12: Run the full sandbox file once more**

Run: `FOUNDRY_TEST_HOST_WORKSPACE_ROOT=/home/snipa14/Development/AgentBuddy pytest services/executor/tests/integration/test_executor_sandbox.py -v`
Expected: all 7 tests PASS (the full file, node test included).

- [ ] **Step 13: Run the full executor unit suite to confirm no regression**

Run: `pytest services/executor/tests/unit -v`
Expected: all PASS (115 + 2 new from Step 6 = 117).

- [ ] **Step 14: Run ruff on every file this task touched**

Run: `ruff check services/executor/imagegen.py services/executor/tests/unit/test_imagegen.py && ruff format --check services/executor/imagegen.py services/executor/tests/unit/test_imagegen.py`
Expected: clean.

- [ ] **Step 15: Commit**

```bash
git -c safe.directory=/workspace add images/workspace.template.Dockerfile services/executor/imagegen.py services/executor/tests/unit/test_imagegen.py images/workspace-python/Dockerfile images/workspace-node/Dockerfile images/workspace-go/Dockerfile
git -c safe.directory=/workspace commit -m "$(cat <<'EOF'
fix(executor): chown package-manager cache dirs at image build time

A freshly created Docker named volume mounted over a path with no
existing image content is created root:root regardless of the
container's runtime --user, so the non-root sandbox user got EACCES on
its first npm/pip install against a brand-new per-project cache volume
(Phase 17). Pre-creating and chowning each stack's cache_dir in the
image lets Docker seed the volume's initial ownership from the image
layer on first mount instead. Found running services/executor's
integration suite against a real daemon for the first time.
EOF
)"
```

(Adjust the `git add` file list if `images/workspace-go/Dockerfile` doesn't exist -- confirm from Step 7's actual output before committing, don't add a path that isn't there.)

---

## Task 11: Chown test-created project directories to the workspace UID (new finding surfaced by Task 10 -- execute before Task 3)

**Controller's note:** Task 10's own fix was verified correct, but the node-stack test then failed at a *later* point: `EACCES` creating `/workspace/node_modules` -- the project's own bind-mounted directory, not the cache volume Task 10 fixes. Investigated directly: this is **not a new production bug**. `services/orchestrator/memory/project_memory.py`'s `chown_to_workspace_uid()` (and its extensive docstring) documents this exact failure mode as already found and fixed in production, some time ago: any process that creates `projects/<project_id>/` while running as root (the Orchestrator's own Dockerfile runs it as root by design) leaves that directory root-owned at mode 755, which blocks the non-root UID-1000 workspace container from creating anything inside it. `services/orchestrator/registry/registration.py` already calls `chown_to_workspace_uid()` right after every real project directory is created, in production. `services/executor/containers.py` already defines the matching `WORKSPACE_UID = 1000` constant this chown must target.

This is purely a **test-fixture gap, not a design decision**: `test_executor_sandbox.py`'s `project_id`/`node_project_id` fixtures create their directories with plain `Path.mkdir()`, never replicating the chown step every real project directory gets in production. That gap was invisible until this session, because this integration suite never ran against a live Docker daemon before. The fix mirrors the already-established, already-documented production pattern -- it does not introduce a new one, and does not touch `services/orchestrator/` (cross-service decoupling stays intact; `services/executor/containers.py` already exports the UID constant this fix needs, same-package).

**Files:**
- Modify: `services/executor/tests/integration/test_executor_sandbox.py` (`project_id` and `node_project_id` fixtures)

**Interfaces:**
- Consumes: `services.executor.containers.WORKSPACE_UID` (already exists, `services/executor/containers.py:44`).
- Produces: nothing new consumed by later tasks -- Task 3 re-verifies the full suite and will now see the node test pass for real.

- [ ] **Step 1: Confirm the gap directly**

Run: `FOUNDRY_TEST_HOST_WORKSPACE_ROOT=/home/snipa14/Development/AgentBuddy pytest services/executor/tests/integration/test_executor_sandbox.py::test_node_stack_run_command_installs_dependencies_from_package_json -v --timeout=300`
Expected: FAIL with `EACCES ... mkdir '/workspace/node_modules'` (this environment's Docker daemon is already reachable and `foundry/workspace-node:20` is already built from Task 10 -- this run should be fast, no rebuild needed).

- [ ] **Step 2: Add the chown helper and wire it into both fixtures**

In `services/executor/tests/integration/test_executor_sandbox.py`, add this import alongside the existing `services.executor.*` imports:

```python
from services.executor.containers import WORKSPACE_UID
```

Add this helper function near `_make_project_roots` (after it, before the fixtures that will call it):

```python
def _chown_to_workspace_uid_if_root(path: Path) -> None:
    """Chowns *path* to the workspace container's UID, mirroring production.

    services/orchestrator/registry/registration.py does this for every real
    project directory immediately after creating it (via
    services.orchestrator.memory.project_memory.chown_to_workspace_uid) --
    a directory created by a root process (the Orchestrator's own container
    runs as root) is otherwise left root-owned at mode 755, which blocks
    the non-root UID-1000 workspace container from creating anything inside
    it. This test's own directories need the same treatment to faithfully
    reproduce production's actual permission state; a non-root test process
    can't chown to an arbitrary UID at all, so this is a no-op in that case.

    Args:
        path: The directory to chown.
    """
    if os.geteuid() == 0:
        os.chown(path, WORKSPACE_UID, WORKSPACE_UID)
```

Update the `project_id` fixture:

```python
@pytest.fixture
def project_id(project_roots):
    new_project_id = f"proj-{uuid.uuid4().hex[:8]}"
    project_dir = project_roots.local / new_project_id
    project_dir.mkdir()
    _chown_to_workspace_uid_if_root(project_dir)
    return new_project_id
```

Update the `node_project_id` fixture:

```python
@pytest.fixture
def node_project_id(project_roots, node_workspace_image):
    new_project_id = f"proj-{uuid.uuid4().hex[:8]}"
    project_dir = project_roots.local / new_project_id
    project_dir.mkdir()
    _chown_to_workspace_uid_if_root(project_dir)
    (project_dir / "package.json").write_text(
        '{"name": "sandbox-check", "version": "1.0.0",'
        ' "dependencies": {"left-pad": "1.3.0"}}'
    )
    return new_project_id
```

- [ ] **Step 3: Re-run the node-stack test**

Run: `FOUNDRY_TEST_HOST_WORKSPACE_ROOT=/home/snipa14/Development/AgentBuddy pytest services/executor/tests/integration/test_executor_sandbox.py::test_node_stack_run_command_installs_dependencies_from_package_json -v --timeout=300`
Expected: PASS. If it still fails, report BLOCKED with the actual error rather than guessing at a third fix -- this specific EACCES-on-node_modules diagnosis should be fully resolved by this change; a different failure here means the diagnosis needs revisiting, not patching further.

- [ ] **Step 4: Run the full sandbox file**

Run: `FOUNDRY_TEST_HOST_WORKSPACE_ROOT=/home/snipa14/Development/AgentBuddy pytest services/executor/tests/integration/test_executor_sandbox.py -v`
Expected: all 7 tests PASS.

- [ ] **Step 5: Run the full executor unit suite to confirm no regression**

Run: `pytest services/executor/tests/unit -v`
Expected: all PASS (117, unchanged from Task 10 -- this task adds no new unit test, it's an integration-fixture fix mirroring an already-tested production helper).

- [ ] **Step 6: Run ruff**

Run: `ruff check services/executor/tests/integration/test_executor_sandbox.py`
Expected: clean.

- [ ] **Step 7: Commit**

```bash
git -c safe.directory=/workspace add services/executor/tests/integration/test_executor_sandbox.py
git -c safe.directory=/workspace commit -m "$(cat <<'EOF'
fix(executor): chown test project directories to the workspace UID

This test's project_id/node_project_id fixtures created directories with
plain mkdir(), never replicating the chown step every real project
directory gets in production
(services/orchestrator/registry/registration.py's
chown_to_workspace_uid()). A directory left root-owned at mode 755 blocks
the non-root UID-1000 workspace container from creating anything inside
it -- surfaced as EACCES creating node_modules once the previous two
fixes (DooD path visibility, cache-volume ownership) got the node-stack
test past its earlier failure points. Mirrors the already-established
production pattern; introduces no new one.
EOF
)"
```
