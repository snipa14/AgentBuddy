"""Shared fakes for per-project subgraph tests.

A ScriptedProvider/FakeRouter pair mocking the LLM at the
model_router.resolve() boundary, per .claude/rules/testing.md -- these
tests exercise the graph's structure and interrupt/resume mechanics, not
Planner prompt quality (see services/orchestrator/agents/tests/unit/
test_planner.py for that).
"""

from services.orchestrator.model_router.schemas import CompletionResult

READY_RESPONSE = "## Status\nready\n"

EMPTY_DRAFT_RESPONSE = (
    "## Summary\nA minimal test project.\n\n"
    "## Goals\n\n## Milestones\n\n## Risks\n\n## Assumptions\n"
)

NO_FINDINGS_CRITIQUE_RESPONSE = "## Findings\n\n## Additional Risks\n\n## Additional Assumptions\n"


class ScriptedProvider:
    """Returns one canned response per call, in order."""

    name = "fake"

    def __init__(self, responses: list[str]):
        self._responses = list(responses)

    def supports(self, capability):
        return True

    async def complete(self, request):
        content = self._responses.pop(0)
        return CompletionResult(content=content, model="fake-model", provider_name="fake")


class FakeRouter:
    """Always resolves the "planner" role to the same ScriptedProvider."""

    def __init__(self, responses: list[str]):
        self._provider = ScriptedProvider(responses)

    def resolve(self, role):
        return self._provider


def fast_track_router(rounds: int = 1) -> FakeRouter:
    """A router that sails the Planner straight through: ready immediately,
    an empty-but-valid draft, and a critique with no findings.

    Args:
        rounds: How many full Planner passes (one per project started
            through this router) to script -- each pass consumes exactly
            the three responses above, in order.
    """
    responses = [READY_RESPONSE, EMPTY_DRAFT_RESPONSE, NO_FINDINGS_CRITIQUE_RESPONSE] * rounds
    return FakeRouter(responses)
