"""Unit tests for tools.py's pure pytest-output parsing helpers (no Docker needed)."""

from services.executor.tools import _parse_coverage_pct, _parse_pytest_summary


def test_parse_pytest_summary_all_passed():
    assert _parse_pytest_summary("5 passed in 0.42s") == (5, 0)


def test_parse_pytest_summary_mixed_results():
    assert _parse_pytest_summary("3 passed, 2 failed in 1.10s") == (3, 2)


def test_parse_pytest_summary_with_errors_counts_as_failed():
    assert _parse_pytest_summary("1 passed, 1 error in 0.05s") == (1, 1)


def test_parse_pytest_summary_failed_before_passed_still_sums_both():
    assert _parse_pytest_summary("2 failed, 3 passed in 1.0s") == (3, 2)


def test_parse_pytest_summary_no_match_returns_zero_zero():
    assert _parse_pytest_summary("no tests ran") == (0, 0)


def test_parse_coverage_pct_extracts_total_percentage():
    output = (
        "Name       Stmts   Miss  Cover\n"
        "--------------------------------\n"
        "app.py         10      2    80%\n"
        "--------------------------------\n"
        "TOTAL          10      2    80%\n"
    )

    assert _parse_coverage_pct(output) == 80.0


def test_parse_coverage_pct_returns_none_when_absent():
    assert _parse_coverage_pct("no coverage report here") is None
