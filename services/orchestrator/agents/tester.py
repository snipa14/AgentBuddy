"""Tester agent — runs tests inside a workspace container and enforces coverage gates.

Calls the Executor's run_tests endpoint, parses results (pass/fail counts,
coverage percentage), and evaluates against the project's configured coverage
threshold using one of three gate modes: hard (fail immediately), soft (warn
but pass), or configurable per-project.

Per .claude/rules/testing.md: mocks at model_router.resolve() boundary; uses
RecordingExecutorClient to assert on exact tool call sequences.
"""

import logging
import re

from pydantic import BaseModel, Field

from services.orchestrator.executor_client import ExecutorClient

logger = logging.getLogger(__name__)

_FRAMEWORK_FOR_STACK: dict[str, str] = {"python": "pytest", "node": "npm"}

_COVERAGE_ROW_RE = re.compile(
    r"^(?P<file>\S+\.py)[ \t]+\d+[ \t]+\d+[ \t]+\d+%"
    r"(?:[ \t]+(?P<missing>[\d,\- \t]+?))?[ \t]*$",
    re.MULTILINE,
)


class TestOutcome(BaseModel):
    """The result of running tests and evaluating the coverage gate."""

    passed: int
    failed: int
    coverage_pct: float | None
    report: str
    gate_mode: str = Field(description="'hard', 'soft', or 'configurable'")
    verdict: str = Field(
        description="'pass' if tests pass and coverage meets threshold; "
        "'fail' otherwise"
    )


class TestFailure(BaseModel):
    """A single test failure with file, line, and message."""

    file: str | None = Field(default=None, description="File path where the test failed")
    line: int | None = Field(default=None, description="Line number of the failure")
    message: str = Field(description="Test failure message or traceback excerpt")


def _parse_failing_tests(report: str) -> list[TestFailure]:
    """Extracts failing test details from a pytest report string.

    Looks for patterns like 'FAILED tests/test_file.py::test_name' and
    extracts file path, line number (if available), and the failure message.

    Args:
        report: The raw pytest output/report text.

    Returns:
        A list of TestFailure objects describing each failing test.
    """
    failures = []
    for line in report.splitlines():
        stripped = line.strip()
        if not stripped.startswith("FAILED"):
            continue

        # Parse "FAILED path::test_name - message" or "FAILED path:line - message"
        rest = stripped.removeprefix("FAILED ").strip()
        file_part = None
        test_name = None
        message = ""

        if "::" in rest:
            parts = rest.split("::", 1)
            file_part = parts[0].strip()
            remainder = parts[1].strip()
            if " - " in remainder:
                test_parts = remainder.split(" - ", 1)
                test_name = test_parts[0].strip()
                message = test_parts[1].strip()
            else:
                test_name = remainder
        elif ":" in rest and "/" in rest:
            # Fallback: "path:line - message"
            parts = rest.split(":", 1)
            file_part = parts[0].strip()
            if " - " in parts[1]:
                message = parts[1].split(" - ", 1)[1].strip()

        failures.append(
            TestFailure(
                file=file_part,
                line=None,
                message=message or f"{file_part}::{test_name}" if test_name else (message or rest),
            )
        )

    return failures


def _parse_uncovered_lines(report: str) -> list[str]:
    """Extracts per-file uncovered line ranges from a coverage.py term-missing report.

    Looks for per-file rows in the ``--cov-report=term-missing`` table (e.g.
    ``services/foo/bar.py    20    3    85%   12-14``) and returns one
    "<file>: <missing ranges>" entry per file that has at least one
    uncovered line. Files with no ``Missing`` column value (fully covered)
    and the ``TOTAL`` summary row are excluded.

    Args:
        report: The raw pytest + coverage.py output text.

    Returns:
        A list of "<file>: <line-ranges>" strings, one per file with
        uncovered lines. Empty if the report has no per-file breakdown or
        every file is fully covered.
    """
    uncovered = []
    for match in _COVERAGE_ROW_RE.finditer(report):
        missing = (match.group("missing") or "").strip()
        if not missing:
            continue
        uncovered.append(f"{match.group('file')}: {missing}")
    return uncovered


def _evaluate_gate(
    passed: int,
    failed: int,
    coverage_pct: float | None,
    threshold: int,
    gate_mode: str = "hard",
) -> TestOutcome:
    """Evaluates test results against the configured coverage gate.

    Args:
        passed: Number of tests that passed.
        failed: Number of tests that failed.
        coverage_pct: Coverage percentage from pytest-cov, or None if unavailable.
        threshold: Minimum required coverage percentage (0-100).
        gate_mode: How to enforce the coverage gate — 'hard' fails on any
            shortfall, 'soft' warns but passes, 'configurable' delegates to
            project config (treated as hard for now).

    Returns:
        A TestOutcome with verdict and all result fields.
    """
    if failed > 0:
        return TestOutcome(
            passed=passed,
            failed=failed,
            coverage_pct=coverage_pct,
            report="",
            gate_mode=gate_mode,
            verdict="fail",
        )

    # All tests passed — check coverage threshold
    if coverage_pct is not None and coverage_pct < threshold:
        if gate_mode == "soft":
            logger.warning(
                "coverage %.1f%% below threshold %d%% (soft mode)",
                coverage_pct, threshold,
            )
            return TestOutcome(
                passed=passed,
                failed=failed,
                coverage_pct=coverage_pct,
                report="",
                gate_mode=gate_mode,
                verdict="pass",
            )

        # hard or configurable — fail below threshold
        logger.info(
            "coverage %.1f%% below threshold %d%% (%s mode)",
            coverage_pct, threshold, gate_mode,
        )
        return TestOutcome(
            passed=passed,
            failed=failed,
            coverage_pct=coverage_pct,
            report="",
            gate_mode=gate_mode,
            verdict="fail",
        )

    return TestOutcome(
        passed=passed,
        failed=failed,
        coverage_pct=coverage_pct,
        report="",
        gate_mode=gate_mode,
        verdict="pass",
    )


async def run_tests(
    executor_client: ExecutorClient,
    project_id: str,
    threshold: int = 80,
    gate_mode: str = "hard",
    stack: str = "python",
) -> TestOutcome:
    """Runs the test suite inside a workspace container and evaluates results.

    Calls the Executor's run_tests endpoint (pytest + coverage.py, or `npm
    test` for a node-stack project -- ADR-028), parses the output into
    pass/fail counts and coverage percentage, then evaluates against the
    configured threshold using the specified gate mode.

    Args:
        executor_client: The Executor client for running tests in the workspace.
        project_id: The project identifier.
        threshold: Minimum required test coverage percentage (0-100).
        gate_mode: Coverage enforcement mode — 'hard' fails below threshold,
            'soft' warns but passes, 'configurable' uses project config.
        stack: The project's declared stack -- selects the test framework
            (pytest for python, `npm test` for node) and the Executor's
            workspace image/cache volume. A node-stack project has no
            coverage report parsed yet, so its coverage_pct is always None
            (the gate treats missing coverage data as passing, regardless
            of gate_mode -- see _evaluate_gate).

    Returns:
        A TestOutcome with verdict ('pass' or 'fail'), coverage_pct, and all
        result fields populated from the Executor's response.
    """
    framework = _FRAMEWORK_FOR_STACK.get(stack, "pytest")
    result = await executor_client.run_tests(project_id, framework=framework, stack=stack)

    outcome = _evaluate_gate(
        passed=result.passed,
        failed=result.failed,
        coverage_pct=result.coverage_pct,
        threshold=threshold,
        gate_mode=gate_mode,
    )
    outcome.report = result.report

    logger.info(
        "project %s: tests %d passed, %d failed, coverage %.1f%% (verdict=%s)",
        project_id,
        result.passed,
        result.failed,
        result.coverage_pct or 0.0,
        outcome.verdict,
    )

    return outcome
