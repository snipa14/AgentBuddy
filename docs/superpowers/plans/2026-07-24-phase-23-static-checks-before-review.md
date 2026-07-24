# Phase 23 — Deterministic Checks Before LLM Review Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reviewer (LLM) tokens are only spent on code that already passes the free deterministic checks — lint/typecheck/build failures loop straight back to the Coder with the raw tool output as feedback (ADR-046).

**Architecture:** A new `node_static_checks` sits on the `coder → reviewer` edge in the project graph. It runs the project's stack-specific `check_commands` from `config/stacks.yaml` inside the workspace container via the existing `run_command` Executor tool — no new Executor surface. The check-running logic lives in a langgraph-free `agents/static_checks.py` module (the same layering as `agents/tester.py`), so its tests run in this sandbox; the graph node in `nodes.py` is a thin wrapper. Failures reopen the most recently completed task (status back to `in_progress`, `attempts + 1`), so `MAX_TASK_ATTEMPTS` and the `blocked` semantics are unchanged.

**Tech Stack:** Python 3.12, Pydantic v2, LangGraph (node wiring only), pytest.

## Global Constraints

- **Depends on Phase 22 being complete**: `config/stacks.yaml`, `services/orchestrator/stacks.py` (`StacksConfig`, `StackCheck`, `load_stacks_config`), and the `ruff`-equipped python workspace image must exist.
- `node_static_checks` makes **zero LLM calls** — never touch `cost_usd_session`/`cost_usd_weekly`, never reference a model name (CLAUDE.md model-routing rule).
- Security boundary untouched: checks run through `ExecutorClient.run_command` only; no new Executor endpoint, no Docker SDK outside `services/executor/`.
- Code conventions (CLAUDE.md): no inline comments; Google-style docstrings; no bare `except`; Pydantic for all external data; type hints everywhere. Node functions prefixed `node_`.
- Git: commits directly on `release/v1`; Conventional Commits per task; phase-end commit `Phase 23: deterministic static checks before LLM review`, tagged `phase-23-done`.
- Sandbox reality: `pytest`/`pydantic` run here; `langgraph`/`langchain_core`/`yaml` do not. `agents/static_checks.py` tests run in-sandbox; `nodes.py`/`build.py` changes and their tests are verified by rigorous hand-trace here and executed for real in CI. Each task states which is which.
- Testing rules (.claude/rules/testing.md): structure-not-content assertions; mock at the `model_router.resolve()` boundary, not deeper.

---

### Task 1: The check runner (`agents/static_checks.py`)

**Files:**
- Create: `services/orchestrator/agents/static_checks.py`
- Test: `services/orchestrator/agents/tests/unit/test_static_checks.py`

**Interfaces:**
- Consumes: `services/orchestrator/stacks.py`'s `StacksConfig`/`StackCheck` (Phase 22 Task 7); `ExecutorClient.run_command(project_id, command, timeout=None, stack="python") -> CommandResult` where `CommandResult` has `stdout`, `stderr`, `exit_code`.
- Produces: `StaticCheckFailure`, `StaticCheckOutcome` (with `.passed` property), `run_static_checks(executor_client, project_id, stack, stacks_config) -> StaticCheckOutcome`, `TOOL_MISSING_EXIT_CODES`. Task 2's `node_static_checks` consumes exactly these.

- [ ] **Step 1: Write the failing tests**

`services/orchestrator/agents/tests/unit/test_static_checks.py`:

```python
"""Unit tests for the deterministic static-check runner (ADR-046).

These tests run in any environment: they need only pydantic and a stubbed
ExecutorClient (no langgraph, no yaml, no Docker).
"""

import asyncio

from services.orchestrator.agents.static_checks import (
    StaticCheckOutcome,
    run_static_checks,
)
from services.orchestrator.stacks import StackCheck, StackConfig, StacksConfig


class _StubCommandResult:
    def __init__(self, exit_code: int, stdout: str = "", stderr: str = ""):
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr


class _StubExecutorClient:
    """Maps argv tuples to canned results; records every call.

    Unmapped commands return exit 0 so file-presence probes default to
    "present" unless a test overrides them.
    """

    def __init__(self, results: dict[tuple[str, ...], _StubCommandResult]):
        self._results = results
        self.calls: list[list[str]] = []

    async def run_command(self, project_id, command, timeout=None, stack="python"):
        self.calls.append(list(command))
        return self._results.get(tuple(command), _StubCommandResult(0))


def _registry(checks: list[StackCheck]) -> StacksConfig:
    return StacksConfig(
        stacks={
            "python": StackConfig(
                image="foundry/workspace-python:3.12",
                base_image="python:3.12-slim",
                check_commands=checks,
            )
        }
    )


def _run(client, stack="python", registry=None) -> StaticCheckOutcome:
    return asyncio.run(run_static_checks(client, "proj1", stack, registry))


def test_all_checks_pass_returns_passed_outcome():
    registry = _registry([StackCheck(command=["ruff", "check", "."])])
    client = _StubExecutorClient({("ruff", "check", "."): _StubCommandResult(0)})
    outcome = _run(client, registry=registry)
    assert outcome.passed
    assert outcome.ran == [["ruff", "check", "."]]
    assert outcome.failures == []


def test_failing_check_captures_output():
    registry = _registry([StackCheck(command=["ruff", "check", "."])])
    client = _StubExecutorClient(
        {
            ("ruff", "check", "."): _StubCommandResult(
                1, stdout="app.py:3:1: F821 undefined name"
            )
        }
    )
    outcome = _run(client, registry=registry)
    assert not outcome.passed
    assert outcome.failures[0].command == ["ruff", "check", "."]
    assert outcome.failures[0].exit_code == 1
    assert "F821" in outcome.failures[0].output


def test_missing_tool_exit_codes_skip_not_fail():
    registry = _registry([StackCheck(command=["ruff", "check", "."])])
    for code in (126, 127):
        client = _StubExecutorClient(
            {("ruff", "check", "."): _StubCommandResult(code, stderr="not found")}
        )
        outcome = _run(client, registry=registry)
        assert outcome.passed
        assert outcome.failures == []
        assert len(outcome.skipped) == 1


def test_requires_file_absent_skips_without_running_check():
    registry = _registry(
        [
            StackCheck(
                command=["npx", "--no-install", "tsc", "--noEmit"],
                requires_file="tsconfig.json",
            )
        ]
    )
    client = _StubExecutorClient(
        {("test", "-f", "tsconfig.json"): _StubCommandResult(1)}
    )
    outcome = _run(client, registry=registry)
    assert outcome.passed
    assert len(outcome.skipped) == 1
    assert ["npx", "--no-install", "tsc", "--noEmit"] not in client.calls


def test_requires_file_present_runs_check():
    registry = _registry(
        [
            StackCheck(
                command=["npx", "--no-install", "tsc", "--noEmit"],
                requires_file="tsconfig.json",
            )
        ]
    )
    client = _StubExecutorClient(
        {
            ("test", "-f", "tsconfig.json"): _StubCommandResult(0),
            ("npx", "--no-install", "tsc", "--noEmit"): _StubCommandResult(
                2, stdout="error TS2304"
            ),
        }
    )
    outcome = _run(client, registry=registry)
    assert not outcome.passed
    assert "TS2304" in outcome.failures[0].output


def test_unknown_stack_passes_with_nothing_ran():
    registry = _registry([StackCheck(command=["ruff", "check", "."])])
    client = _StubExecutorClient({})
    outcome = _run(client, stack="cobol", registry=registry)
    assert outcome.passed
    assert outcome.ran == []
    assert client.calls == []


def test_multiple_checks_all_run_even_after_failure():
    registry = _registry(
        [
            StackCheck(command=["go", "vet", "./..."]),
            StackCheck(command=["go", "build", "./..."]),
        ]
    )
    client = _StubExecutorClient(
        {
            ("go", "vet", "./..."): _StubCommandResult(1, stderr="vet: bad"),
            ("go", "build", "./..."): _StubCommandResult(1, stderr="build: bad"),
        }
    )
    outcome = _run(client, registry=registry)
    assert len(outcome.failures) == 2
    assert len(outcome.ran) == 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest services/orchestrator/agents/tests/unit/test_static_checks.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'services.orchestrator.agents.static_checks'`

- [ ] **Step 3: Write the implementation**

`services/orchestrator/agents/static_checks.py`:

```python
"""Deterministic static-check runner -- the no-LLM half of ADR-046.

Runs a stack's check_commands from config/stacks.yaml inside the project's
workspace container through the Executor's existing run_command tool. Makes
zero LLM calls by design: cheap deterministic verification happens before
any Reviewer token is spent, and this module is independently testable
without langgraph (the same layering as agents/tester.py).

Skip semantics keep the checks honest on projects that legitimately lack a
tool: a check whose requires_file is absent from the project is skipped
before it runs, and a check whose executable is missing from the workspace
image (exec exit codes 126/127) is skipped after -- neither is a failure.
"""

import logging

from pydantic import BaseModel, Field

from services.orchestrator.stacks import StacksConfig

logger = logging.getLogger(__name__)

TOOL_MISSING_EXIT_CODES = frozenset({126, 127})


class StaticCheckFailure(BaseModel):
    """One failed check command and its raw output.

    Attributes:
        command: The argv list that failed.
        exit_code: The command's exit code.
        output: Combined stdout + stderr, verbatim -- this is the feedback
            the Coder gets, so it is never summarized or truncated here.
    """

    command: list[str]
    exit_code: int
    output: str


class StaticCheckOutcome(BaseModel):
    """The result of running every applicable check for a project.

    Attributes:
        ran: Commands that executed to completion (pass or fail).
        skipped: Human-readable "command -- reason" entries for checks that
            did not apply.
        failures: Every check that ran and exited non-zero.
    """

    ran: list[list[str]] = Field(default_factory=list)
    skipped: list[str] = Field(default_factory=list)
    failures: list[StaticCheckFailure] = Field(default_factory=list)

    @property
    def passed(self) -> bool:
        """True when no applicable check failed."""
        return not self.failures


async def run_static_checks(
    executor_client,
    project_id: str,
    stack: str,
    stacks_config: StacksConfig,
) -> StaticCheckOutcome:
    """Runs a stack's deterministic checks inside the workspace container.

    Args:
        executor_client: The Executor client checks are dispatched through
            (duck-typed to ExecutorClient.run_command, so tests can stub it).
        project_id: The project whose workspace container to use.
        stack: The project's declared stack -- selects both the workspace
            image (via the Executor) and which check_commands apply.
        stacks_config: The loaded stack registry (ADR-045).

    Returns:
        A StaticCheckOutcome; `.passed` is True when nothing applicable
        failed, including when the stack has no registry entry or no
        check_commands at all.
    """
    outcome = StaticCheckOutcome()
    entry = stacks_config.stacks.get(stack)
    if entry is None or not entry.check_commands:
        logger.info(
            "project %s: no static checks registered for stack %r", project_id, stack
        )
        return outcome

    for check in entry.check_commands:
        rendered = " ".join(check.command)
        if check.requires_file is not None:
            probe = await executor_client.run_command(
                project_id, ["test", "-f", check.requires_file], stack=stack
            )
            if probe.exit_code != 0:
                outcome.skipped.append(
                    f"{rendered} -- requires {check.requires_file}, not present"
                )
                logger.info(
                    "project %s: skipping %s (%s absent)",
                    project_id, rendered, check.requires_file,
                )
                continue

        result = await executor_client.run_command(
            project_id, check.command, stack=stack
        )
        if result.exit_code in TOOL_MISSING_EXIT_CODES:
            outcome.skipped.append(
                f"{rendered} -- tool missing (exit {result.exit_code})"
            )
            logger.info(
                "project %s: skipping %s (tool missing)", project_id, rendered
            )
            continue

        outcome.ran.append(list(check.command))
        if result.exit_code != 0:
            outcome.failures.append(
                StaticCheckFailure(
                    command=list(check.command),
                    exit_code=result.exit_code,
                    output=f"{result.stdout}\n{result.stderr}".strip(),
                )
            )

    logger.info(
        "project %s: static checks ran=%d skipped=%d failed=%d",
        project_id, len(outcome.ran), len(outcome.skipped), len(outcome.failures),
    )
    return outcome
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest services/orchestrator/agents/tests/unit/test_static_checks.py -v`
Expected: all PASS (this suite fully executes in-sandbox).

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/agents/static_checks.py services/orchestrator/agents/tests/unit/test_static_checks.py
git commit -m "feat(orchestrator): deterministic static-check runner (ADR-046)"
```

---

### Task 2: The graph node and routing (`nodes.py`)

**Files:**
- Modify: `services/orchestrator/graph/nodes.py`
- Test: `services/orchestrator/graph/tests/unit/test_nodes.py` (additions)

**Interfaces:**
- Consumes: Task 1's `run_static_checks`/`StaticCheckOutcome`; existing `MAX_TASK_ATTEMPTS`, `_replace_task`, `ProjectState`.
- Produces: `node_static_checks(state, executor_client, stacks_config) -> dict[str, Any]`, `route_after_static_checks(state) -> str`, and a changed `route_after_coder` (returns `"static_checks"` where it returned `"reviewer"`). Task 3 wires these into `build.py`.

- [ ] **Step 1: Write the failing tests**

Add to `services/orchestrator/graph/tests/unit/test_nodes.py`, following that file's existing stub/fixture style for `ProjectState` and `ExecutorClient` (adapt names to what is already there — read the file first):

```python
def _checked_state(task_queue, stack="python"):
    state = _base_state()
    state["phase"] = "reviewing"
    state["stack"] = stack
    state["task_queue"] = task_queue
    return state


def _passing_checks_client():
    return _StubExecutorClient(default_exit_code=0)


def _failing_checks_client(output="app.py:1:1: F821 undefined name"):
    return _StubExecutorClient(default_exit_code=1, default_stdout=output)


def test_static_checks_pass_keeps_reviewing_phase(two_stack_registry):
    state = _checked_state([{"description": "t", "status": "done", "attempts": 1}])
    update = node_static_checks(
        state, executor_client=_passing_checks_client(), stacks_config=two_stack_registry
    )
    assert update.get("phase", state["phase"]) == "reviewing"


def test_static_checks_failure_reopens_last_done_task_with_feedback(two_stack_registry):
    queue = [
        {"description": "first", "status": "done", "attempts": 1},
        {"description": "second", "status": "done", "attempts": 0},
    ]
    state = _checked_state(queue)
    update = node_static_checks(
        state, executor_client=_failing_checks_client(), stacks_config=two_stack_registry
    )
    assert update["phase"] == "implementing"
    assert update["task_queue"][1]["status"] == "in_progress"
    assert update["task_queue"][1]["attempts"] == 1
    assert update["task_queue"][0] == queue[0]
    assert "F821" in update["messages"][0].content


def test_static_checks_exhaustion_blocks(two_stack_registry):
    queue = [{"description": "t", "status": "done", "attempts": 2}]
    state = _checked_state(queue)
    update = node_static_checks(
        state, executor_client=_failing_checks_client(), stacks_config=two_stack_registry
    )
    assert update["phase"] == "blocked"
    assert update["task_queue"][0]["status"] == "blocked"
    assert update["curator_source"] == "static_checks"


def test_static_checks_failure_with_no_done_task_blocks(two_stack_registry):
    state = _checked_state([])
    update = node_static_checks(
        state, executor_client=_failing_checks_client(), stacks_config=two_stack_registry
    )
    assert update["phase"] == "blocked"


def test_route_after_coder_sends_reviewing_to_static_checks():
    state = _base_state()
    state["phase"] = "reviewing"
    assert route_after_coder(state) == "static_checks"


def test_route_after_static_checks_routes_all_three_outcomes():
    state = _base_state()
    state["phase"] = "reviewing"
    assert route_after_static_checks(state) == "reviewer"
    state["phase"] = "implementing"
    assert route_after_static_checks(state) == "coder"
    state["phase"] = "blocked"
    assert route_after_static_checks(state) == "memory_curator"
```

The `two_stack_registry` fixture: add a `conftest.py`-level fixture (or reuse the file's existing helpers) building a `StacksConfig` whose python entry has `check_commands=[StackCheck(command=["ruff", "check", "."])]` — import from `services.orchestrator.stacks`.

- [ ] **Step 2: Verify the tests fail**

In CI: `python3 -m pytest services/orchestrator/graph/tests/unit/test_nodes.py -v` → FAIL (`node_static_checks` undefined). In this sandbox `nodes.py` is unimportable (langgraph) — record that the red step is deferred to CI and proceed on hand-trace discipline: write the test first anyway.

- [ ] **Step 3: Implement in `nodes.py`**

Add imports `from services.orchestrator.agents.static_checks import run_static_checks` and `from services.orchestrator.stacks import StacksConfig`. Then:

```python
def node_static_checks(
    state: ProjectState,
    executor_client: ExecutorClient,
    stacks_config: StacksConfig,
) -> dict[str, Any]:
    """Runs the stack's deterministic checks before any Reviewer token is spent.

    Sits between coder and reviewer (ADR-046). Makes no LLM calls and never
    touches cost tracking. On pass (including nothing applicable to run),
    control continues to the Reviewer exactly as before this node existed.
    On failure, the most recently completed task is reopened -- status back
    to "in_progress" with attempts incremented, honoring MAX_TASK_ATTEMPTS
    and the existing blocked semantics -- and the raw check output is
    attached to messages as the Coder's feedback. A failure with no
    reopenable task (empty or never-completed queue) blocks the project
    rather than letting broken code through to review.

    Args:
        state: The current project state (phase "reviewing" on entry).
        executor_client: The Executor client checks run through.
        stacks_config: The loaded stack registry (ADR-045).

    Returns:
        A state update: {} on pass; task_queue/phase/messages updates on
        failure; a blocked transition when attempts are exhausted.
    """
    outcome = asyncio.run(
        run_static_checks(
            executor_client, state["project_id"], state["stack"], stacks_config
        )
    )
    if outcome.passed:
        logger.info(
            "project %s: static checks passed (%d ran, %d skipped)",
            state["project_id"], len(outcome.ran), len(outcome.skipped),
        )
        return {}

    failure_lines = ["Static checks failed:"]
    for failure in outcome.failures:
        failure_lines.append(
            f"$ {' '.join(failure.command)} (exit {failure.exit_code})\n{failure.output}"
        )
    feedback = AIMessage(content="\n\n".join(failure_lines))

    task_queue = state["task_queue"]
    last_done_index = next(
        (i for i in range(len(task_queue) - 1, -1, -1)
         if task_queue[i]["status"] == "done"),
        None,
    )
    if last_done_index is None:
        logger.warning(
            "project %s: static checks failed with no completed task to reopen",
            state["project_id"],
        )
        return {
            "phase": "blocked",
            "curator_source": "static_checks",
            "messages": [feedback],
        }

    reopened = {
        **task_queue[last_done_index],
        "status": "in_progress",
        "attempts": task_queue[last_done_index]["attempts"] + 1,
    }
    if reopened["attempts"] >= MAX_TASK_ATTEMPTS:
        reopened["status"] = "blocked"
        logger.warning(
            "project %s: static checks exhausted %d attempts, blocking",
            state["project_id"], reopened["attempts"],
        )
        return {
            "task_queue": _replace_task(task_queue, last_done_index, reopened),
            "phase": "blocked",
            "curator_source": "static_checks",
            "messages": [feedback],
        }

    logger.info(
        "project %s: static checks failed (%d finding set(s)), reopening task",
        state["project_id"], len(outcome.failures),
    )
    return {
        "task_queue": _replace_task(task_queue, last_done_index, reopened),
        "phase": "implementing",
        "messages": [feedback],
    }


def route_after_static_checks(state: ProjectState) -> str:
    """Routes to Coder on failure, curator on blocked, Reviewer on pass."""
    if state["phase"] == "blocked":
        return "memory_curator"
    if state["phase"] == "implementing":
        return "coder"
    return "reviewer"
```

Change `route_after_coder`'s reviewing branch to return `"static_checks"` and update its docstring ("routes to the deterministic checks gate when done"). Update the module docstring's node chain sentence to insert `static_checks` between coder and reviewer.

- [ ] **Step 4: Verify**

In-sandbox: hand-trace all seven new tests line-by-line against the implementation (state in the commit message that this was hand-traced). In CI: `python3 -m pytest services/orchestrator/graph/tests/unit/test_nodes.py -v` → all PASS.

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/graph/nodes.py services/orchestrator/graph/tests/unit/test_nodes.py
git commit -m "feat(orchestrator): node_static_checks gates coder output before review (ADR-046)"
```

---

### Task 3: Graph wiring + service/entrypoint plumbing

**Files:**
- Modify: `services/orchestrator/graph/build.py`
- Modify: `services/orchestrator/service.py`
- Modify: `services/orchestrator/main.py`
- Modify: `services/orchestrator/registry/cli.py` (`_start_intake`)
- Modify: `services/orchestrator/tests/unit/test_service.py` (constructor updates)

**Interfaces:**
- Consumes: Task 2's node/route functions; Phase 22's `load_stacks_config` and `STACKS_CONFIG_PATH` convention.
- Produces: `build_project_graph(..., stacks_config: StacksConfig)` (new keyword-only-position parameter after `projects_root`, before `global_cost_threshold_usd` — keep `global_cost_threshold_usd` last-with-default) and `OrchestratorService(..., stacks_config, global_cost_threshold_usd=...)` mirroring it. Phase 24 builds on this exact graph shape.

- [ ] **Step 1: Wire the node in `build.py`**

Add imports (`node_static_checks`, `route_after_static_checks`, `from services.orchestrator.stacks import StacksConfig`). Give `build_project_graph` a `stacks_config: StacksConfig` parameter (document it in the docstring, including that an empty registry disables all checks — every stack then passes through to the Reviewer unchanged). Add the node:

```python
    builder.add_node(
        "static_checks",
        functools.partial(
            node_static_checks,
            executor_client=executor_client,
            stacks_config=stacks_config,
        ),
    )
```

Change the coder edge mapping to:

```python
    builder.add_conditional_edges(
        "coder",
        route_after_coder,
        {
            "coder": "coder",
            "static_checks": "static_checks",
            "memory_curator": "memory_curator",
        },
    )
    builder.add_conditional_edges(
        "static_checks",
        route_after_static_checks,
        {
            "coder": "coder",
            "reviewer": "reviewer",
            "memory_curator": "memory_curator",
        },
    )
```

and update the builder docstring's node-chain description (`coder -> static_checks -> reviewer`).

- [ ] **Step 2: Thread the registry through `service.py`, `main.py`, and `_start_intake`**

`OrchestratorService.__init__` gains `stacks_config: StacksConfig` (positional, after `projects_root`), passes it to `build_project_graph`, documents it. `main.py`: load once beside the models config —

```python
    stacks_config = load_stacks_config(
        Path(os.environ.get("STACKS_CONFIG_PATH", DEFAULT_STACKS_CONFIG_PATH))
    )
```

with `DEFAULT_STACKS_CONFIG_PATH = "config/stacks.yaml"` and `from services.orchestrator.stacks import load_stacks_config` at module top (main.py already imports langgraph-adjacent modules eagerly; no lazy-import dance needed there), then pass it into `OrchestratorService(...)`. `registry/cli.py`'s `_start_intake`: same load inside the function body (it already lazy-imports its dependencies), passed into its `OrchestratorService(...)` call — reuse the `stack_choices` load in `main()` only for the parser; `_start_intake` loads independently because it may be called with a different env.

- [ ] **Step 3: Update `test_service.py`**

Every `OrchestratorService(...)` construction gains a `StacksConfig(stacks={})` argument (import from `services.orchestrator.stacks`). An empty registry means static checks pass through — existing service tests keep their behavior.

- [ ] **Step 4: Verify**

In-sandbox: `python3 -m pytest services/orchestrator/tests/unit -v` (test_service imports langgraph via service.py — if unimportable here, hand-trace and note it; the registry and agents suites still run). In CI: full orchestrator suite green.

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/graph/build.py services/orchestrator/service.py services/orchestrator/main.py services/orchestrator/registry/cli.py services/orchestrator/tests/unit/test_service.py
git commit -m "feat(orchestrator): wire static_checks node into the project graph (ADR-046)"
```

---

### Task 4: Definition-of-Done graph test + phase close-out

**Files:**
- Modify: `services/orchestrator/graph/tests/unit/test_graph_end_to_end.py` (additions)
- Modify: `TODO.md`, `ARCHITECTURE.md` (§4.7/§5 node-chain description gains `static_checks`; ADR-046 already exists)

**Interfaces:**
- Consumes: everything above.

- [ ] **Step 1: Write the DoD test**

Follow `test_graph_end_to_end.py`'s existing conventions for building a compiled graph with stub router/executor (read the file first; reuse its fixtures). The two required tests:

```python
def test_lint_broken_diff_never_reaches_reviewer(...):
    """DoD (ADR-046): a failing check must route back to coder without any
    Reviewer LLM call. The router stub raises AssertionError if the graph
    resolves the 'reviewer' role."""
```

Mechanics: build the graph with a `stacks_config` whose python entry has one check command; stub `ExecutorClient.run_command` to return exit 1 with lint output for the check command (and exit 0 for everything else); stub the router so `resolve("reviewer")` raises `AssertionError("reviewer must not be called")` while other roles return the usual canned stub. Drive the graph to the point where the coder finishes its tasks (reuse the file's existing plan-approval/decomposition scaffolding). Assert the final state has the reopened task (`status == "in_progress"`, `attempts == 1`) and phase `"implementing"`, and that no assertion fired.

```python
def test_clean_diff_reaches_reviewer_exactly_as_before(...):
    """A passing check set must leave the pre-phase behavior intact: the
    reviewer's resolve() is called once and the graph proceeds."""
```

Same scaffolding with the check command returning exit 0; assert the reviewer stub was invoked and the graph advanced past `reviewing`.

- [ ] **Step 2: Verify**

In-sandbox: hand-trace both tests against `build.py`'s wiring and `node_static_checks` (langgraph unavailable); record the trace in the task notes/commit message. In CI: `python3 -m pytest services/orchestrator/graph/tests/unit/test_graph_end_to_end.py -v` → PASS.

- [ ] **Step 3: Phase close-out**

Run everything runnable: `python3 -m pytest services/orchestrator/agents/tests/unit services/orchestrator/registry/tests/unit services/executor/tests/unit -q` → zero FAIL. Check Phase 23's TODO.md boxes with verification notes (runnable vs hand-traced). Update ARCHITECTURE.md's graph description (§4.7 or §5 — wherever the coder→reviewer chain is described) to include `static_checks`. Commit and tag:

```bash
git add TODO.md ARCHITECTURE.md services/orchestrator/graph/tests/unit/test_graph_end_to_end.py
git commit -m "Phase 23: deterministic static checks before LLM review"
git tag phase-23-done
```
