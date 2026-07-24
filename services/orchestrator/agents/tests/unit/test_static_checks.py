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
