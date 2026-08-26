"""Shared LangChain completion helper for LLM-backed agent modules (ADR-025).

Every agent module resolves a `BaseChatModel` from `ModelRouter.resolve()`
and drives it directly: `with_structured_output()` for single-shot structured
responses (Planner, Reviewer, Frontend Reviewer, Memory Curator), `bind_tools()`
for the Coder's tool-calling loop (services.orchestrator.agents.coder). This
module holds the pieces of glue every structured-output call needs: forcing
`include_raw=True` so the raw `AIMessage` -- and its `usage_metadata`, needed
for cost tracking -- comes back alongside the parsed result, without agent
code ever needing to know about token pricing itself (ADR-026); forcing
`method="function_calling"` rather than leaving the method provider-inferred
(ADR-049) -- left unspecified, `ChatOpenAI` silently resolves to OpenAI's
strict `json_schema` structured-output mode for `openai_compatible`
providers, which several real reasoning-mode local models fail on outright
(the model exhausts its turn on its `<think>` block and never enters the
JSON grammar, returning empty content with neither a `parsed` nor a
`refusal` field); and retrying up to `STRUCTURED_OUTPUT_MAX_ATTEMPTS` times
when a provider returns no tool call at all rather than raising on the first
attempt (ADR-051) -- observed as an intermittent, nondeterministic failure
against a real reasoning-mode local model, where a fresh sampling attempt
often succeeds where the last one didn't; a genuine `parsing_error` (the
model called the tool but produced invalid output) is a different failure
class and is never retried. Tool-calling is the structured-output mechanism
every provider in this router supports reliably.
"""

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from pydantic import BaseModel

STRUCTURED_OUTPUT_MAX_ATTEMPTS = 3
"""How many times invoke_structured tries before giving up when a provider
returns parsed=None with no parsing_error (ADR-051)."""


def _sum_usage(usages: list[dict | None]) -> dict | None:
    """Sums usage_metadata dicts across multiple invoke_structured attempts.

    Args:
        usages: usage_metadata from every attempt, including failed ones.

    Returns:
        A combined usage_metadata dict with summed token counts, or None if
        every attempt reported no usage at all.
    """
    real = [usage for usage in usages if usage is not None]
    if not real:
        return None
    return {
        "input_tokens": sum(usage.get("input_tokens", 0) or 0 for usage in real),
        "output_tokens": sum(usage.get("output_tokens", 0) or 0 for usage in real),
        "total_tokens": sum(usage.get("total_tokens", 0) or 0 for usage in real),
    }


async def invoke_structured[ResultT: BaseModel](
    llm: BaseChatModel, schema: type[ResultT], messages: list[BaseMessage]
) -> tuple[ResultT, AIMessage]:
    """Invokes `llm` with a schema-forced structured response.

    Args:
        llm: A chat model resolved from `ModelRouter.resolve()`.
        schema: The Pydantic model the response must validate against.
        messages: The conversation to send.

    Returns:
        A tuple of `(parsed_result, raw_ai_message)`. The raw message's
        `usage_metadata` is the sum of every attempt's usage, including
        failed ones (ADR-051), which callers pass to `ModelRouter.cost_usd()`
        -- kept separate from `parsed_result` so agent code never has to
        reason about pricing.

    Raises:
        ValueError: If the model's response doesn't validate against schema,
            or if every attempt parsed to None without a parsing_error (the
            model never called the expected tool).
    """
    structured_llm = llm.with_structured_output(schema, include_raw=True, method="function_calling")
    usages: list[dict | None] = []
    last_raw: AIMessage | None = None
    for _ in range(STRUCTURED_OUTPUT_MAX_ATTEMPTS):
        response = await structured_llm.ainvoke(messages)
        if response["parsing_error"] is not None:
            raise ValueError(
                f"structured output failed to parse as {schema.__name__}: "
                f"{response['parsing_error']}"
            )
        last_raw = response["raw"]
        usages.append(last_raw.usage_metadata)
        if response["parsed"] is not None:
            if len(usages) == 1:
                return response["parsed"], last_raw
            return response["parsed"], last_raw.model_copy(
                update={"usage_metadata": _sum_usage(usages)}
            )

    raise ValueError(
        f"structured output for {schema.__name__} parsed to None with no "
        f"parsing_error after {STRUCTURED_OUTPUT_MAX_ATTEMPTS} attempts -- the "
        f"model never called the expected tool. Raw response: {last_raw!r}"
    )
