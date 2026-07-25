"""Unit tests for invoke_structured() (ADR-025, ADR-049, ADR-051)."""

import asyncio

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from pydantic import BaseModel

from services.orchestrator.agents.llm import STRUCTURED_OUTPUT_MAX_ATTEMPTS, invoke_structured
from services.orchestrator.agents.tests.unit.router_fakes import (
    FakeChatModel,
    ScriptedStructured,
    usage,
)


class _Schema(BaseModel):
    value: str


def test_invoke_structured_returns_parsed_and_raw_on_success():
    raw = AIMessage(content="", usage_metadata=usage())
    llm = FakeChatModel([ScriptedStructured(parsed=_Schema(value="x"), raw=raw)])

    parsed, ai_message = asyncio.run(
        invoke_structured(llm, _Schema, [HumanMessage(content="hi")])
    )

    assert parsed == _Schema(value="x")
    assert ai_message is raw


def test_invoke_structured_raises_on_parsing_error():
    raw = AIMessage(content="not json", usage_metadata=usage())
    llm = FakeChatModel(
        [ScriptedStructured(parsed=None, raw=raw, parsing_error=ValueError("bad json"))]
    )

    with pytest.raises(ValueError, match="failed to parse"):
        asyncio.run(invoke_structured(llm, _Schema, [HumanMessage(content="hi")]))


def test_invoke_structured_raises_when_every_attempt_parses_to_none():
    """Some providers return include_raw=True's dict with parsed=None and
    parsing_error=None -- neither a clean success nor LangChain's own
    exception path (observed against a real ChatAnthropic-shaped local
    server that responded without calling the expected tool). This must not
    silently propagate None to callers, which would crash deep inside a
    graph node instead of surfacing a clear error at the call site. Every
    attempt up to STRUCTURED_OUTPUT_MAX_ATTEMPTS is retried (ADR-051) before
    this raises."""
    raw = AIMessage(content="I have a question instead", usage_metadata=usage())
    llm = FakeChatModel(
        [
            ScriptedStructured(parsed=None, raw=raw, parsing_error=None)
            for _ in range(STRUCTURED_OUTPUT_MAX_ATTEMPTS)
        ]
    )

    with pytest.raises(ValueError, match="parsed to None"):
        asyncio.run(invoke_structured(llm, _Schema, [HumanMessage(content="hi")]))


def test_invoke_structured_retries_after_a_transient_failure_and_aggregates_usage():
    """A model that fails to call the tool once, then succeeds on a later
    attempt, must not surface an error -- and the returned usage_metadata
    must include the failed attempt's tokens too, so cost tracking isn't
    silently undercounted for the tokens a failed attempt still consumed
    (ADR-051)."""
    empty_raw = AIMessage(content=[], usage_metadata=usage(input_tokens=50, output_tokens=20))
    success_raw = AIMessage(content="", usage_metadata=usage(input_tokens=10, output_tokens=10))
    llm = FakeChatModel(
        [
            ScriptedStructured(parsed=None, raw=empty_raw, parsing_error=None),
            ScriptedStructured(parsed=_Schema(value="x"), raw=success_raw),
        ]
    )

    parsed, ai_message = asyncio.run(
        invoke_structured(llm, _Schema, [HumanMessage(content="hi")])
    )

    assert parsed == _Schema(value="x")
    assert ai_message.usage_metadata["input_tokens"] == 60
    assert ai_message.usage_metadata["output_tokens"] == 30
    assert ai_message.usage_metadata["total_tokens"] == 90
