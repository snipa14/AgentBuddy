"""Shared ModelRouter/BaseChatModel fakes for agent unit tests (ADR-025).

Fakes the `BaseChatModel` surface agent code actually drives -- `.ainvoke()`
for the Coder's tool-calling loop, `.bind_tools()` to accept the loop's tool
list, and `.with_structured_output(schema, include_raw=True)` for every
other agent's single-shot structured response -- rather than mocking at the
old text-completion `Provider.complete()` boundary. Per .claude/rules/
testing.md, this still mocks at the model_router.resolve() boundary, just
one layer further out now that `resolve()` itself returns a `BaseChatModel`.
"""

from dataclasses import dataclass

from langchain_core.messages import AIMessage
from pydantic import BaseModel


@dataclass
class ScriptedStructured:
    """One scripted `with_structured_output(schema, include_raw=True)` response."""

    parsed: BaseModel | None
    raw: AIMessage
    parsing_error: Exception | None = None


@dataclass
class ScriptedToolCall:
    """One scripted `bind_tools(...).ainvoke()` turn -- a message, maybe with tool calls."""

    message: AIMessage


class _FakeStructuredRunnable:
    """Returned by `FakeChatModel.with_structured_output()`."""

    def __init__(self, model: "FakeChatModel", schema: type[BaseModel], include_raw: bool):
        self._model = model
        self._schema = schema
        self._include_raw = include_raw

    async def ainvoke(self, messages, **kwargs):
        self._model.requests.append(messages)
        response = self._model._pop()
        if not isinstance(response, ScriptedStructured):
            raise TypeError(
                f"scripted a {type(response).__name__} for a with_structured_output() call"
            )
        if not self._include_raw:
            if response.parsing_error is not None:
                raise response.parsing_error
            return response.parsed
        return {
            "raw": response.raw,
            "parsed": response.parsed,
            "parsing_error": response.parsing_error,
        }


class FakeChatModel:
    """Fakes `BaseChatModel`'s `.ainvoke()`/`.bind_tools()`/`.with_structured_output()`.

    Scripted with a list of responses consumed in order -- `AIMessage` for
    plain `.ainvoke()` calls (the Coder's tool-calling loop) and
    `ScriptedStructured` for `.with_structured_output()` calls (every other
    agent). Records every request it receives in `requests` so tests can
    assert on what was actually sent to the model.
    """

    def __init__(self, responses: list):
        self._responses = list(responses)
        self.requests: list = []
        self.bound_tools: dict | None = None

    def _pop(self):
        return self._responses.pop(0)

    def bind_tools(self, tools):
        self.bound_tools = {tool.name: tool for tool in tools}
        return self

    async def ainvoke(self, messages, **kwargs):
        self.requests.append(messages)
        response = self._pop()
        if isinstance(response, ScriptedStructured):
            raise TypeError("scripted a ScriptedStructured response for a plain ainvoke() call")
        return response

    def with_structured_output(self, schema: type[BaseModel], include_raw: bool = False):
        return _FakeStructuredRunnable(self, schema, include_raw)


def usage(input_tokens: int = 10, output_tokens: int = 10) -> dict:
    """Builds a LangChain-shaped `UsageMetadata` dict for a scripted response."""
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
    }


def structured(parsed: BaseModel, *, input_tokens: int = 10, output_tokens: int = 10):
    """Convenience constructor for a happy-path ScriptedStructured response."""
    return ScriptedStructured(
        parsed=parsed,
        raw=AIMessage(content="", usage_metadata=usage(input_tokens, output_tokens)),
    )


class FakeRouter:
    """Always resolves any role to the same FakeChatModel."""

    def __init__(self, responses: list | None = None):
        self.model = FakeChatModel(responses or [])
        self.cost_calls: list = []

    def resolve(self, role):
        return self.model

    def cost_usd(self, role, usage_metadata):
        self.cost_calls.append((role, usage_metadata))
        if usage_metadata is None:
            return 0.0
        input_tokens = usage_metadata.get("input_tokens", 0) or 0
        output_tokens = usage_metadata.get("output_tokens", 0) or 0
        return input_tokens / 1_000_000 * 3.0 + output_tokens / 1_000_000 * 15.0
