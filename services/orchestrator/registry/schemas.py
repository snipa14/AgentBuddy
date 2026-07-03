"""Pydantic schema for the project registry (ARCHITECTURE.md §4.6, ADR-013)."""

from typing import Literal

from pydantic import BaseModel

DEFAULT_COVERAGE_THRESHOLD = 80

ProjectMode = Literal["greenfield", "brownfield"]
PrMode = Literal["auto", "manual", "configurable"]
CoverageGateMode = Literal["hard", "soft", "configurable"]
ProjectStatus = Literal["active", "archived"]

DEFAULT_COVERAGE_GATE_MODE: CoverageGateMode = "hard"


class ProjectRecord(BaseModel):
    """A single project's entry in `projects.yaml`."""

    id: str
    name: str
    mode: ProjectMode
    path: str
    git_remote: str | None = None
    stack: str
    coverage_threshold: int = DEFAULT_COVERAGE_THRESHOLD
    pr_mode: PrMode = "manual"
    coverage_gate_mode: CoverageGateMode = DEFAULT_COVERAGE_GATE_MODE
    cost_ceiling_usd_weekly: float | None = None
    status: ProjectStatus = "active"
