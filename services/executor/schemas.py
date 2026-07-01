"""Pydantic result schemas returned by the Executor's typed tool functions."""

from pydantic import BaseModel


class CommandResult(BaseModel):
    """The outcome of a run_command call inside a workspace container."""

    stdout: str
    stderr: str
    exit_code: int


class FileEntry(BaseModel):
    """A single entry returned by list_files."""

    name: str
    is_dir: bool


class TestResult(BaseModel):
    """The outcome of a run_tests call inside a workspace container."""

    passed: int
    failed: int
    coverage_pct: float | None
    report: str
