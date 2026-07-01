"""Pydantic schemas for provider-agnostic completion requests and results."""

from typing import Literal

from pydantic import BaseModel, Field


class CompletionMessage(BaseModel):
    """A single message in a completion request's conversation history."""

    role: Literal["system", "user", "assistant"]
    content: str


class CompletionRequest(BaseModel):
    """A role-agnostic request passed to a Provider's complete() method."""

    messages: list[CompletionMessage]
    max_tokens: int = 4096
    temperature: float = Field(default=0.2, ge=0.0, le=1.0)


class TokenUsage(BaseModel):
    """Token accounting returned alongside a completion, used for cost tracking."""

    input_tokens: int
    output_tokens: int


class CompletionResult(BaseModel):
    """The normalized result of a Provider.complete() call, regardless of backend."""

    content: str
    model: str
    provider_name: str
    usage: TokenUsage | None = None
