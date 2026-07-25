"""Unit tests for invoke_structured() (ADR-025, ADR-049)."""

import asyncio

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from pydantic import BaseModel

from services.orchestrator.agents.llm import invoke_structured
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


def test_invoke_structured_raises_when_parsed_is_none_with_no_parsing_error():
    """Some providers return include_raw=True's dict with parsed=None and
    parsing_error=None -- neither a clean success nor LangChain's own
    exception path (observed against a real ChatAnthropic-shaped local
    server that responded without calling the expected tool). This must not
    silently propagate None to callers, which would crash deep inside a
    graph node instead of surfacing a clear error at the call site."""
    raw = AIMessage(content="I have a question instead", usage_metadata=usage())
    llm = FakeChatModel([ScriptedStructured(parsed=None, raw=raw, parsing_error=None)])

    with pytest.raises(ValueError, match="parsed to None"):
        asyncio.run(invoke_structured(llm, _Schema, [HumanMessage(content="hi")]))
