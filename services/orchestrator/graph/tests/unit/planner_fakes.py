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

from langchain_core.messages import AIMessage, AIMessageChunk
from pydantic import BaseModel

from services.orchestrator.agents.coder import CoderTask, _TaskList
from services.orchestrator.agents.memory_curator import CurationResult
from services.orchestrator.agents.planner import CritiqueResult, DraftPlan, InterviewTurn
from services.orchestrator.agents.reviewer import ReviewResult


@dataclass
class ScriptedStructured:
    """One scripted `with_structured_output(schema, include_raw=True)` response."""

    parsed: BaseModel | None
    raw: AIMessage


def _split_into_chunks(message: AIMessage) -> list[AIMessageChunk]:
    """Splits a scripted `AIMessage` into `AIMessageChunk`s for `.astream()`
    (mirrors agents/tests/unit/router_fakes.py's copy -- see that module's
    docstring for why the two fake tiers don't share fixtures)."""
    if message.tool_calls:
        return [
            AIMessageChunk(
                content=message.content,
                tool_calls=message.tool_calls,
                usage_metadata=message.usage_metadata,
            )
        ]
    words = message.content.split(" ") if message.content else [""]
    chunks = [AIMessageChunk(content=f"{word} ") for word in words[:-1]]
    chunks.append(AIMessageChunk(content=words[-1], usage_metadata=message.usage_metadata))
    return chunks


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

CURATION_OK = structured(CurationResult(summary_markdown="Stub curation summary."))


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

    async def astream(self, messages, **kwargs):
        response = self._pop()
        if isinstance(response, ScriptedStructured):
            raise TypeError("scripted a ScriptedStructured response for a plain astream() call")
        for chunk in _split_into_chunks(response):
            yield chunk

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


def fast_track_router_with_curation(rounds: int = 1) -> FakeRouter:
    """Like fast_track_router(), but also scripts a CurationResult response at
    each of the three memory_curator checkpoints (after plan_wait, after
    testing, after merge_wait) that fire for real whenever `messages` is
    non-empty.

    fast_track_router() itself deliberately has no curator responses: every
    one of its other consumers (test_graph_end_to_end.py,
    test_integrator.py, test_concurrency.py's own hand-rolled scripts) seeds
    `new_project_state()` without `initial_message`, so `messages` stays
    empty for the whole run and node_memory_curator's `if not phase_events`
    guard skips the LLM call entirely -- adding curator responses there
    would be unused dead entries at best.

    OrchestratorService.handle_webhook(), however, always seeds
    `initial_message` (ADR-035), so `messages` is non-empty from the first
    curator checkpoint onward for every test that drives the graph via
    OrchestratorService (test_service.py, test_api.py) -- those tests need
    this variant instead.
    """
    responses = [
        READY_TURN,
        EMPTY_DRAFT,
        NO_FINDINGS_CRITIQUE,
        CURATION_OK,
        ONE_TASK,
        CODER_DONE,
        REVIEWER_APPROVE,
        CURATION_OK,
        CURATION_OK,
    ] * rounds
    return FakeRouter(responses)
