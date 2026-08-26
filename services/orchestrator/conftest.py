"""Root pytest fixtures shared across every services/orchestrator test tier."""

import pytest

from services.orchestrator import progress_buffer


@pytest.fixture(autouse=True)
def _reset_progress_buffer():
    """Clears the process-global progress buffer (ADR-027) between tests.

    progress_buffer._buffers is deliberately process-local, in-memory state
    keyed by project_id (see that module's docstring) -- fine in production
    where project_ids are unique, but many test files across this tree
    reuse literal placeholder ids like "proj-1", so without this reset,
    add_progress() calls from one test leak into another test's
    get_chunks_since() assertions whenever they share a project_id and run
    in the same pytest process.
    """
    progress_buffer._buffers.clear()
    yield
    progress_buffer._buffers.clear()
