"""Shared fakes for per-project subgraph tests.

A ScriptedProvider/FakeRouter pair mocking the LLM at the
model_router.resolve() boundary, per .claude/rules/testing.md -- these
tests exercise the graph's structure and interrupt/resume mechanics, not
Planner/Coder prompt quality (see services/orchestrator/agents/tests/unit/
for that).
"""

from services.orchestrator.model_router.schemas import CompletionResult

READY_RESPONSE = "## Status\nready\n"

EMPTY_DRAFT_RESPONSE = (
    "## Summary\nA minimal test project.\n\n"
    "## Goals\n\n## Milestones\n\n## Risks\n\n## Assumptions\n"
)

NO_FINDINGS_CRITIQUE_RESPONSE = "## Findings\n\n## Additional Risks\n\n## Additional Assumptions\n"

ONE_TASK_RESPONSE = "## Tasks\nDo the trivial thing\n"

CODER_DONE_RESPONSE = "## Action\ndone\n"

REVIEWER_APPROVE_RESPONSE = "## Verdict\napprove\n\n## Findings\n"


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
    """Always resolves any role to the same ScriptedProvider."""

    def __init__(self, responses: list[str]):
        self._provider = ScriptedProvider(responses)

    def resolve(self, role):
        return self._provider


def fast_track_router(rounds: int = 1) -> FakeRouter:
    """A router that sails a project straight through Planner and Coder:
    ready immediately, an empty-but-valid draft, a critique with no
    findings, exactly one decomposed task, and that task immediately
    reporting "done" -- so the Coder's ExecutorClient is never actually
    called (safe to pass a placeholder for tests that don't care about it).

    Each round now also includes a reviewer approve response after the coder.

    Args:
        rounds: How many full Planner+Coder+Reviewer passes (one per project
            started through this router) to script -- each pass consumes exactly
            the six responses below, in order.
    """
    responses = [
        READY_RESPONSE,
        EMPTY_DRAFT_RESPONSE,
        NO_FINDINGS_CRITIQUE_RESPONSE,
        ONE_TASK_RESPONSE,
        CODER_DONE_RESPONSE,
        REVIEWER_APPROVE_RESPONSE,
    ] * rounds
    return FakeRouter(responses)
