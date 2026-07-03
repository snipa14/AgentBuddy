"""Reviewer reliability test against a real model (TODO.md Phase 10 Definition of Done).

"Both fixture cases pass reliably across at least 5 repeated runs -- LLM-based
checks need a consistency bar, not just a single lucky pass" (TODO.md), and
.claude/rules/testing.md spells out the same bar for Reviewer/Tester
specifically: plant a real defect, run 5 times, assert it's flagged in at
least 4 of them. That's a claim about actual model behavior, so unlike the
unit suite (tests/unit/test_reviewer.py, which mocks the provider at
model_router.resolve() per .claude/rules/testing.md's general rule) this
test makes real Claude API calls -- it's the one place that verifies the
Reviewer's prompt genuinely gets a real model to notice the planted bug.

Requires an explicit FOUNDRY_RUN_REVIEWER_RELIABILITY_TEST=1 opt-in (this
test makes ~10 real, billed Claude API calls -- merely having a credential
in the environment isn't enough signal to fire it automatically, see the
anthropic_api_key fixture's docstring for why), plus a Claude API key from
/run/secrets/claude_api_key (the real Docker secrets mount) or the
CLAUDE_API_KEY/ANTHROPIC_API_KEY env vars. Skips cleanly otherwise, same as
services/orchestrator/agents/tests/integration/test_coder_end_to_end.py skips
cleanly with no reachable Docker daemon.

Run with:
    FOUNDRY_RUN_REVIEWER_RELIABILITY_TEST=1 pytest -m integration \\
        services/orchestrator/agents/tests/integration/test_reviewer_reliability.py
"""

import asyncio
import os
from pathlib import Path

import pytest

from services.orchestrator.agents.reviewer import review_changes
from services.orchestrator.executor_client import FileEntry
from services.orchestrator.model_router.config import load_models_config
from services.orchestrator.model_router.router import ModelRouter

pytestmark = pytest.mark.integration

RUNS = 5
MIN_RELIABLE = 4

CLEAN_FIXTURE = {
    "account.py": (
        "def withdraw(balance: int, amount: int) -> int:\n"
        "    if amount > balance:\n"
        "        raise ValueError('insufficient funds')\n"
        "    return balance - amount\n"
    )
}

DEFECT_FIXTURE = {
    "account.py": (
        "DB_PASSWORD = 'sup3rSecretPassw0rd!'\n\n"
        "def withdraw(balance: int, amount: int) -> int:\n"
        "    return balance - amount\n"
    )
}


class _FixtureExecutorClient:
    """Fakes just enough of ExecutorClient for review_changes() to read fixture files."""

    def __init__(self, files: dict[str, str]):
        self._files = files

    async def read_file(self, project_id: str, path: str) -> str:
        return self._files[path]

    async def list_files(self, project_id: str, path: str = "."):
        return [FileEntry(name=name, is_dir=False) for name in self._files]


RUN_ENV_VAR = "FOUNDRY_RUN_REVIEWER_RELIABILITY_TEST"


@pytest.fixture(scope="module")
def anthropic_api_key() -> str:
    """Resolves a Claude API key, but only if this test was explicitly opted into.

    Presence of a Claude/Anthropic credential in the environment isn't enough
    signal on its own to fire ten real, billed API calls -- this suite has
    previously run inside a Claude Code session where ANTHROPIC_API_KEY is
    always set (it's that session's own credential, not a Foundry deployment
    secret), so gating on the key alone caused this test to silently make
    real network calls. Requiring a distinct, explicit opt-in env var makes
    "yes, actually spend real API quota" a decision a human has to make, not
    an accident of what happens to be in the environment.
    """
    if os.environ.get(RUN_ENV_VAR) != "1":
        pytest.skip(
            f"set {RUN_ENV_VAR}=1 to run this test -- it makes real, billed Claude API calls"
        )
    secret_path = Path("/run/secrets/claude_api_key")
    if secret_path.exists():
        return secret_path.read_text().strip()
    for env_var in ("CLAUDE_API_KEY", "ANTHROPIC_API_KEY"):
        value = os.environ.get(env_var)
        if value:
            return value
    pytest.skip("no Claude API key available (/run/secrets/claude_api_key or CLAUDE_API_KEY/ANTHROPIC_API_KEY)")


@pytest.fixture(scope="module")
def router(anthropic_api_key) -> ModelRouter:
    config = load_models_config(Path("config/models.claude-only.yaml"))
    return ModelRouter(config, anthropic_api_key=anthropic_api_key)


async def _run_once(router: ModelRouter, files: dict[str, str]) -> str:
    result, _ = await review_changes(router, _FixtureExecutorClient(files), "proj-reliability")
    return result.verdict


def _run_many(router: ModelRouter, files: dict[str, str], count: int) -> list[str]:
    return asyncio.run(_run_many_async(router, files, count))


async def _run_many_async(router: ModelRouter, files: dict[str, str], count: int) -> list[str]:
    return [await _run_once(router, files) for _ in range(count)]


def test_reviewer_flags_hardcoded_credential_reliably(router):
    verdicts = _run_many(router, DEFECT_FIXTURE, RUNS)
    flagged = sum(1 for v in verdicts if v == "changes-requested")

    assert flagged >= MIN_RELIABLE, (
        f"expected the hardcoded-credential fixture flagged in >= {MIN_RELIABLE}/{RUNS} runs, "
        f"got {flagged}/{RUNS} (verdicts: {verdicts})"
    )


def test_reviewer_approves_clean_code_reliably(router):
    verdicts = _run_many(router, CLEAN_FIXTURE, RUNS)
    approved = sum(1 for v in verdicts if v == "approve")

    assert approved >= MIN_RELIABLE, (
        f"expected the clean fixture approved in >= {MIN_RELIABLE}/{RUNS} runs, "
        f"got {approved}/{RUNS} (verdicts: {verdicts})"
    )
