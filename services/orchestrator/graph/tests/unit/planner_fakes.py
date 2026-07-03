"""Shared fakes for per-project subgraph tests.

A FakeChatModel/FakeRouter pair mocking the LLM at the model_router.resolve()
boundary, per .claude/rules/testing.md -- these tests exercise the graph's
structure and interrupt/resume mechanics, not Planner/Coder prompt quality
(see services/orchestrator/agents/tests/unit/ for that). Deliberately a
separate copy of the fakes from agents/tests/unit/router_fakes.py rather than
a shared import, so the two test tiers never couple to each other's fixture
changes.
"""

from dataclasses import dataclass

from langchain_core.messages import AIMessage
from pydantic import BaseModel

from services.orchestrator.agents.coder import CoderTask, _TaskList
from services.orchestrator.agents.planner import CritiqueResult, DraftPlan, InterviewTurn
from services.orchestrator.agents.reviewer import ReviewResult


@dataclass
class ScriptedStructured:
    """One scripted `with_structured_output(schema, include_raw=True)` response."""

    parsed: BaseModel | None
    raw: AIMessage


def _usage(input_tokens: int = 10, output_tokens: int = 10) -> dict:
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
    }


def structured(parsed: BaseModel) -> ScriptedStructured:
    """Convenience constructor for a happy-path ScriptedStructured response."""
    return ScriptedStructured(parsed=parsed, raw=AIMessage(content="", usage_metadata=_usage()))


READY_TURN = structured(InterviewTurn(status="ready"))

EMPTY_DRAFT = structured(DraftPlan(summary="A minimal test project."))

NO_FINDINGS_CRITIQUE = structured(CritiqueResult())

ONE_TASK = structured(_TaskList(tasks=[CoderTask(description="Do the trivial thing")]))

CODER_DONE = AIMessage(content="done", tool_calls=[], usage_metadata=_usage())

REVIEWER_APPROVE = structured(ReviewResult(verdict="approve", findings=[]))


class _FakeStructuredRunnable:
    def __init__(self, model: "FakeChatModel", schema, include_raw: bool):
        self._model = model
        self._include_raw = include_raw

    async def ainvoke(self, messages, **kwargs):
        response = self._model._pop()
        if not isinstance(response, ScriptedStructured):
            raise TypeError(
                f"scripted a {type(response).__name__} for a with_structured_output() call"
            )
        if not self._include_raw:
            return response.parsed
        return {"raw": response.raw, "parsed": response.parsed, "parsing_error": None}


class FakeChatModel:
    """Fakes `BaseChatModel`'s `.ainvoke()`/`.bind_tools()`/`.with_structured_output()`."""

    def __init__(self, responses: list):
        self._responses = list(responses)

    def _pop(self):
        return self._responses.pop(0)

    def bind_tools(self, tools):
        return self

    async def ainvoke(self, messages, **kwargs):
        response = self._pop()
        if isinstance(response, ScriptedStructured):
            raise TypeError("scripted a ScriptedStructured response for a plain ainvoke() call")
        return response

    def with_structured_output(self, schema, include_raw: bool = False):
        return _FakeStructuredRunnable(self, schema, include_raw)


class FakeRouter:
    """Always resolves any role to the same FakeChatModel."""

    def __init__(self, responses: list):
        self._model = FakeChatModel(responses)

    def resolve(self, role):
        return self._model

    def cost_usd(self, role, usage_metadata):
        if usage_metadata is None:
            return 0.0
        input_tokens = usage_metadata.get("input_tokens", 0) or 0
        output_tokens = usage_metadata.get("output_tokens", 0) or 0
        return input_tokens / 1_000_000 * 3.0 + output_tokens / 1_000_000 * 15.0


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
        READY_TURN,
        EMPTY_DRAFT,
        NO_FINDINGS_CRITIQUE,
        ONE_TASK,
        CODER_DONE,
        REVIEWER_APPROVE,
    ] * rounds
    return FakeRouter(responses)
