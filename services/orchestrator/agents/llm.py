"""Shared LangChain completion helper for LLM-backed agent modules (ADR-025).

Every agent module resolves a `BaseChatModel` from `ModelRouter.resolve()`
and drives it directly: `with_structured_output()` for single-shot structured
responses (Planner, Reviewer, Frontend Reviewer, Memory Curator), `bind_tools()`
for the Coder's tool-calling loop (services.orchestrator.agents.coder). This
module holds the two pieces of glue every structured-output call needs:
forcing `include_raw=True` so the raw `AIMessage` -- and its `usage_metadata`,
needed for cost tracking -- comes back alongside the parsed result, without
agent code ever needing to know about token pricing itself (ADR-026); and
forcing `method="function_calling"` rather than leaving the method
provider-inferred (ADR-049) -- left unspecified, `ChatOpenAI` silently
resolves to OpenAI's strict `json_schema` structured-output mode for
`openai_compatible` providers, which several real reasoning-mode local
models fail on outright (the model exhausts its turn on its `<think>` block
and never enters the JSON grammar, returning empty content with neither a
`parsed` nor a `refusal` field). Tool-calling is the structured-output
mechanism every provider in this router supports reliably.
"""

from typing import TypeVar

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from pydantic import BaseModel

ResultT = TypeVar("ResultT", bound=BaseModel)


async def invoke_structured(
    llm: BaseChatModel, schema: type[ResultT], messages: list[BaseMessage]
) -> tuple[ResultT, AIMessage]:
    """Invokes `llm` with a schema-forced structured response.

    Args:
        llm: A chat model resolved from `ModelRouter.resolve()`.
        schema: The Pydantic model the response must validate against.
        messages: The conversation to send.

    Returns:
        A tuple of `(parsed_result, raw_ai_message)`. The raw message carries
        `usage_metadata`, which callers pass to `ModelRouter.cost_usd()` --
        kept separate from `parsed_result` so agent code never has to reason
        about pricing.

    Raises:
        ValueError: If the model's response doesn't validate against schema.
    """
    structured_llm = llm.with_structured_output(schema, include_raw=True, method="function_calling")
    response = await structured_llm.ainvoke(messages)
    if response["parsing_error"] is not None:
        raise ValueError(
            f"structured output failed to parse as {schema.__name__}: {response['parsing_error']}"
        )
    return response["parsed"], response["raw"]
