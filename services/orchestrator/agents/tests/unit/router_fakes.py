"""Shared ModelRouter fakes for agent unit tests.

Mocks the LLM at the model_router.resolve() boundary per
.claude/rules/testing.md -- a ScriptedProvider returns one canned response
per call, in order, standing in for a real multi-turn conversation.
"""

from services.orchestrator.model_router.schemas import CompletionResult


class ScriptedProvider:
    """Returns one canned response per call, in order.

    Records every request it receives in `requests` so tests can assert on
    what was actually sent to the model (e.g. that a system prompt was
    included), not just on the canned response coming back out.
    """

    name = "fake"

    def __init__(self, responses: list[str]):
        self._responses = list(responses)
        self.requests: list = []

    def supports(self, capability):
        return True

    async def complete(self, request):
        self.requests.append(request)
        content = self._responses.pop(0)
        return CompletionResult(content=content, model="fake-model", provider_name="fake")


class FakeRouter:
    """Always resolves any role to the same ScriptedProvider."""

    def __init__(self, responses: list[str]):
        self.provider = ScriptedProvider(responses)

    def resolve(self, role):
        return self.provider
