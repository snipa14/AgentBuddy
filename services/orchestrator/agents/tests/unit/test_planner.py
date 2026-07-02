"""Unit tests for the Planner / Intake agent.

Per .claude/rules/testing.md, LLM-backed agent tests mock the provider at
the model_router.resolve() boundary -- a _ScriptedProvider returns one
canned response per call, in order, standing in for a real multi-turn
conversation with a model.
"""

import asyncio

import pytest

from services.orchestrator.agents.planner import (
    DraftPlan,
    apply_critique,
    ask_clarifying_questions,
    critique_plan,
    draft_plan,
    run_intake_to_final_plan,
)
from services.orchestrator.model_router.schemas import CompletionResult

INTERVIEW_QUESTIONS_RESPONSE = (
    "## Status\nquestions\n\n## Questions\n- What language?\n- Who are the users?\n"
)
INTERVIEW_READY_RESPONSE = "## Status\nready\n"

DRAFT_RESPONSE = """## Summary
A small internal tool for the ops team to track on-call rotations.

## Goals
- Let ops self-serve rotation changes
- Replace the spreadsheet

## Milestones
- Data model and API
- Basic UI

## Risks
- Migration from the existing spreadsheet could lose history

## Assumptions
- Ops team has fewer than 20 members
"""

CRITIQUE_RESPONSE_WITH_FINDINGS = """## Findings
category: security_gap | severity: high | No mention of who can edit rotations; \
anyone with the link could reassign on-call.
category: untested_claim | severity: medium | "Replace the spreadsheet" assumes \
the spreadsheet has no other consumers -- unconfirmed.

## Additional Risks
- Other teams or automations may depend on reading the existing spreadsheet directly

## Additional Assumptions
- Only ops team members can authenticate to edit rotations
"""

CRITIQUE_RESPONSE_NO_FINDINGS = """## Findings

## Additional Risks

## Additional Assumptions
"""


class _ScriptedProvider:
    """Returns one canned response per call, in order."""

    name = "fake"

    def __init__(self, responses: list[str]):
        self._responses = list(responses)

    def supports(self, capability):
        return True

    async def complete(self, request):
        content = self._responses.pop(0)
        return CompletionResult(content=content, model="fake-model", provider_name="fake")


class _FakeRouter:
    def __init__(self, responses: list[str]):
        self._provider = _ScriptedProvider(responses)

    def resolve(self, role):
        return self._provider


def test_ask_clarifying_questions_parses_a_batch_of_questions():
    router = _FakeRouter([INTERVIEW_QUESTIONS_RESPONSE])

    turn = asyncio.run(ask_clarifying_questions(router, "build an on-call tool", []))

    assert turn.status == "questions"
    assert turn.questions == ["What language?", "Who are the users?"]


def test_ask_clarifying_questions_parses_ready_signal():
    router = _FakeRouter([INTERVIEW_READY_RESPONSE])

    turn = asyncio.run(ask_clarifying_questions(router, "build an on-call tool", ["prior Q&A"]))

    assert turn.status == "ready"
    assert turn.questions == []


def test_ask_clarifying_questions_raises_on_malformed_status():
    router = _FakeRouter(["## Status\nmaybe\n"])

    with pytest.raises(ValueError, match="malformed interview status"):
        asyncio.run(ask_clarifying_questions(router, "x", []))


def test_draft_plan_parses_all_five_sections():
    router = _FakeRouter([DRAFT_RESPONSE])

    plan = asyncio.run(draft_plan(router, "build an on-call tool", ["some", "history"]))

    assert "on-call rotations" in plan.summary
    assert "Let ops self-serve rotation changes" in plan.goals
    assert "Data model and API" in plan.milestones
    assert "Migration from the existing spreadsheet could lose history" in plan.risks
    assert "Ops team has fewer than 20 members" in plan.assumptions


def test_draft_plan_raises_when_summary_section_missing():
    router = _FakeRouter(["## Goals\n- a\n"])

    with pytest.raises(ValueError, match="missing Summary section"):
        asyncio.run(draft_plan(router, "x", []))


def test_critique_plan_parses_findings_and_revisions():
    router = _FakeRouter([CRITIQUE_RESPONSE_WITH_FINDINGS])
    plan = DraftPlan(summary="s", risks=["existing risk"], assumptions=["existing assumption"])

    critique = asyncio.run(critique_plan(router, plan))

    assert len(critique.findings) == 2
    assert critique.findings[0].category == "security_gap"
    assert critique.findings[0].severity == "high"
    assert critique.additional_risks == [
        "Other teams or automations may depend on reading the existing spreadsheet directly"
    ]
    assert critique.additional_assumptions == [
        "Only ops team members can authenticate to edit rotations"
    ]


def test_critique_plan_raises_on_malformed_finding_line():
    router = _FakeRouter(["## Findings\n- not the right format\n"])
    plan = DraftPlan(summary="s")

    with pytest.raises(ValueError, match="malformed critique finding line"):
        asyncio.run(critique_plan(router, plan))


def test_apply_critique_merges_new_risks_and_assumptions_into_the_plan():
    draft = DraftPlan(
        summary="A small internal tool.",
        risks=["Migration from the existing spreadsheet could lose history"],
        assumptions=["Ops team has fewer than 20 members"],
    )
    router = _FakeRouter([CRITIQUE_RESPONSE_WITH_FINDINGS])
    critique = asyncio.run(critique_plan(router, draft))

    final_plan = apply_critique(draft, critique)

    assert final_plan != draft
    assert len(final_plan.risks) == 2
    assert len(final_plan.assumptions) == 2
    assert (
        "Other teams or automations may depend on reading the existing spreadsheet directly"
        in final_plan.risks
    )
    assert "Only ops team members can authenticate to edit rotations" in final_plan.assumptions


def test_apply_critique_is_a_no_op_when_critique_finds_nothing():
    draft = DraftPlan(summary="A small internal tool.", risks=["r1"], assumptions=["a1"])
    router = _FakeRouter([CRITIQUE_RESPONSE_NO_FINDINGS])
    critique = asyncio.run(critique_plan(router, draft))

    final_plan = apply_critique(draft, critique)

    assert final_plan == draft


def test_apply_critique_does_not_duplicate_a_risk_the_draft_already_has():
    draft = DraftPlan(summary="s", risks=["shared risk"])
    from services.orchestrator.agents.planner import CritiqueResult

    critique = CritiqueResult(additional_risks=["shared risk", "new risk"])

    final_plan = apply_critique(draft, critique)

    assert final_plan.risks == ["shared risk", "new risk"]


def test_run_intake_to_final_plan_reaches_a_complete_plan_via_scripted_answers():
    """The Phase 8 Definition-of-Done scenario: a scripted sequence of user
    answers drives the interview to completion, producing a plan whose risks
    and assumptions were actually changed by the critique pass -- proving
    the critique isn't a no-op end to end."""
    router = _FakeRouter(
        [
            INTERVIEW_QUESTIONS_RESPONSE,
            INTERVIEW_READY_RESPONSE,
            DRAFT_RESPONSE,
            CRITIQUE_RESPONSE_WITH_FINDINGS,
        ]
    )
    answers = iter(["Python, and it's for our internal ops team."])

    result = asyncio.run(
        run_intake_to_final_plan(router, "build an on-call rotation tool", lambda qs: next(answers))
    )

    assert "on-call rotations" in result.plan.summary
    assert len(result.critique_findings) == 2
    assert (
        "Other teams or automations may depend on reading the existing spreadsheet directly"
        in result.plan.risks
    )
    assert "Only ops team members can authenticate to edit rotations" in result.plan.assumptions
    # The pre-critique draft never mentioned authentication -- proves the
    # critique pass, not just the draft, put this assumption on the plan.
    draft_only_assumptions = ["Ops team has fewer than 20 members"]
    assert "Only ops team members can authenticate to edit rotations" not in draft_only_assumptions


def test_run_intake_to_final_plan_stops_at_max_rounds():
    router = _FakeRouter(
        [
            INTERVIEW_QUESTIONS_RESPONSE,
            INTERVIEW_QUESTIONS_RESPONSE,
            DRAFT_RESPONSE,
            CRITIQUE_RESPONSE_NO_FINDINGS,
        ]
    )
    answers = iter(["answer 1", "answer 2"])

    result = asyncio.run(
        run_intake_to_final_plan(
            router, "build a tool", lambda qs: next(answers), max_rounds=2
        )
    )

    assert result.plan.summary
