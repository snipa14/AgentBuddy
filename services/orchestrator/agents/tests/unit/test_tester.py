"""Unit tests for the Tester agent.

Per .claude/rules/testing.md, mocks at model_router.resolve() boundary; uses
RecordingExecutorClient to assert on exact tool call sequences. Tests cover:
- _evaluate_gate with all three modes (hard/soft/configurable)
- _parse_failing_tests extracting details from pytest reports
- run_tests end-to-end with mocked executor client
"""

import asyncio

import pytest

from services.orchestrator.agents.tester import (
    TestOutcome,
    _evaluate_gate,
    _parse_failing_tests,
    _parse_uncovered_lines,
    run_tests,
)


class RecordingExecutorClient:
    """Fakes ExecutorClient, recording every call made to it in order."""

    def __init__(self, test_result=None):
        self.test_result = test_result
        self.calls: list[tuple] = []

    async def run_tests(self, project_id: str, framework: str = "pytest", stack: str = "python"):
        self.calls.append(("run_tests", project_id, framework, stack))
        if self.test_result is not None:
            return self.test_result
        from services.orchestrator.executor_client import TestResult as ExecutorTestResult

        return ExecutorTestResult(passed=3, failed=0, coverage_pct=100.0, report="")


# -- _evaluate_gate tests ------------------------------------------------------

def test_evaluate_gate_passes_when_all_tests_pass_and_coverage_meets_threshold():
    outcome = _evaluate_gate(
        passed=5, failed=0, coverage_pct=85.0, threshold=80, gate_mode="hard"
    )
    assert outcome.verdict == "pass"
    assert outcome.coverage_pct == 85.0


def test_evaluate_gate_fails_when_tests_fail():
    outcome = _evaluate_gate(
        passed=4, failed=1, coverage_pct=90.0, threshold=80, gate_mode="hard"
    )
    assert outcome.verdict == "fail"


def test_evaluate_gate_hard_mode_blocks_on_low_coverage():
    outcome = _evaluate_gate(
        passed=5, failed=0, coverage_pct=60.0, threshold=80, gate_mode="hard"
    )
    assert outcome.verdict == "fail"


def test_evaluate_gate_soft_mode_passes_on_low_coverage():
    outcome = _evaluate_gate(
        passed=5, failed=0, coverage_pct=60.0, threshold=80, gate_mode="soft"
    )
    assert outcome.verdict == "pass"


def test_evaluate_gate_configurable_mode_blocks_on_low_coverage():
    outcome = _evaluate_gate(
        passed=5, failed=0, coverage_pct=60.0, threshold=80, gate_mode="configurable"
    )
    assert outcome.verdict == "fail"


def test_evaluate_gate_passes_when_no_coverage_data():
    outcome = _evaluate_gate(
        passed=5, failed=0, coverage_pct=None, threshold=80, gate_mode="hard"
    )
    assert outcome.verdict == "pass"


def test_evaluate_gate_fails_when_no_tests_ran_hard_mode():
    outcome = _evaluate_gate(
        passed=0, failed=0, coverage_pct=None, threshold=80, gate_mode="hard"
    )
    assert outcome.verdict == "fail"
    assert "no tests" in outcome.report.lower()


def test_evaluate_gate_fails_when_no_tests_ran_soft_mode_does_not_rescue():
    outcome = _evaluate_gate(
        passed=0, failed=0, coverage_pct=None, threshold=80, gate_mode="soft"
    )
    assert outcome.verdict == "fail"
    assert "no tests" in outcome.report.lower()


# -- _parse_failing_tests tests ------------------------------------------------

def test_parse_failing_tests_extracts_single_failure():
    report = (
        "2 passed\n"
        "FAILED tests/test_models.py::test_user_creation - AssertionError: expected 1, got 0\n"
    )
    failures = _parse_failing_tests(report)
    assert len(failures) == 1
    assert failures[0].file == "tests/test_models.py"
    assert "AssertionError" in failures[0].message


def test_parse_failing_tests_does_not_fabricate_a_line_number():
    report = "FAILED tests/test_models.py::test_user_creation - AssertionError\n"
    failures = _parse_failing_tests(report)
    assert failures[0].line is None


def test_parse_failing_tests_extracts_multiple_failures():
    report = (
        "FAILED tests/test_a.py::test_one - AssertionError\n"
        "FAILED tests/test_b.py::test_two - ValueError: bad input\n"
    )
    failures = _parse_failing_tests(report)
    assert len(failures) == 2
    assert failures[0].file == "tests/test_a.py"
    assert failures[1].file == "tests/test_b.py"


def test_parse_failing_tests_returns_empty_on_clean_report():
    report = "5 passed\n"
    failures = _parse_failing_tests(report)
    assert failures == []


# -- _parse_uncovered_lines tests -----------------------------------------------

def test_parse_uncovered_lines_extracts_missing_ranges_per_file():
    report = (
        "Name                      Stmts   Miss  Cover   Missing\n"
        "-------------------------------------------------------\n"
        "services/foo/bar.py          20      3    85%   12-14\n"
        "services/foo/baz.py          15      0   100%\n"
        "-------------------------------------------------------\n"
        "TOTAL                         35      3    91%\n"
    )
    uncovered = _parse_uncovered_lines(report)
    assert uncovered == ["services/foo/bar.py: 12-14"]


def test_parse_uncovered_lines_returns_empty_when_fully_covered():
    report = (
        "Name                  Stmts   Miss  Cover\n"
        "------------------------------------------\n"
        "services/foo/bar.py      20      0  100%\n"
        "------------------------------------------\n"
        "TOTAL                    20      0  100%\n"
    )
    assert _parse_uncovered_lines(report) == []


def test_parse_uncovered_lines_returns_empty_without_a_coverage_table():
    assert _parse_uncovered_lines("5 passed in 0.10s\n") == []


# -- run_tests end-to-end tests ------------------------------------------------

def test_run_tests_calls_executor_and_returns_outcome():
    from services.orchestrator.executor_client import TestResult as ExecutorTestResult

    executor = RecordingExecutorClient(
        test_result=ExecutorTestResult(passed=3, failed=0, coverage_pct=92.5, report="")
    )
    outcome = asyncio.run(run_tests(executor, "proj-1", threshold=80))
    assert outcome.verdict == "pass"
    assert outcome.coverage_pct == 92.5
    assert ("run_tests", "proj-1", "pytest", "python") in executor.calls


def test_run_tests_uses_npm_framework_for_a_node_stack_project():
    executor = RecordingExecutorClient()

    asyncio.run(run_tests(executor, "proj-1", threshold=80, stack="node"))

    assert ("run_tests", "proj-1", "npm", "node") in executor.calls


def test_run_tests_fails_and_explains_when_no_tests_collected():
    from services.orchestrator.executor_client import TestResult as ExecutorTestResult

    executor = RecordingExecutorClient(
        test_result=ExecutorTestResult(passed=0, failed=0, coverage_pct=None, report="")
    )
    outcome = asyncio.run(run_tests(executor, "proj-1", threshold=80))
    assert outcome.verdict == "fail"
    assert "no tests" in outcome.report.lower()


def test_run_tests_no_tests_collected_preserves_raw_executor_diagnostics():
    from services.orchestrator.executor_client import TestResult as ExecutorTestResult

    executor = RecordingExecutorClient(
        test_result=ExecutorTestResult(
            passed=0,
            failed=0,
            coverage_pct=None,
            report="ImportError while loading conftest.py: No module named 'foo'",
        )
    )
    outcome = asyncio.run(run_tests(executor, "proj-1", threshold=80))
    assert outcome.verdict == "fail"
    assert "no tests" in outcome.report.lower()
    assert "ImportError while loading conftest.py: No module named 'foo'" in outcome.report


def test_run_tests_returns_fail_when_coverage_below_threshold():
    from services.orchestrator.executor_client import TestResult as ExecutorTestResult

    executor = RecordingExecutorClient(
        test_result=ExecutorTestResult(passed=3, failed=0, coverage_pct=50.0, report="")
    )
    outcome = asyncio.run(run_tests(executor, "proj-1", threshold=80))
    assert outcome.verdict == "fail"
