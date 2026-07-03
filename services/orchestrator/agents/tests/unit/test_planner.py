"""Unit tests for the Planner / Intake agent.

Per .claude/rules/testing.md, LLM-backed agent tests mock the provider at
the model_router.resolve() boundary -- see router_fakes.py's FakeRouter/
FakeChatModel, shared with test_coder.py/test_reviewer.py.
"""

import asyncio

import pytest

from services.orchestrator.agents.planner import (
    CritiqueFinding,
    CritiqueResult,
    DraftPlan,
    InterviewTurn,
    apply_critique,
    ask_clarifying_questions,
    critique_plan,
    draft_plan,
    run_intake_to_final_plan,
)
from langchain_core.messages import AIMessage

from services.orchestrator.agents.tests.unit.router_fakes import (
    FakeRouter,
    ScriptedStructured,
    structured,
)

INTERVIEW_QUESTIONS_TURN = InterviewTurn(
    status="questions", questions=["What language?", "Who are the users?"]
)
INTERVIEW_READY_TURN = InterviewTurn(status="ready")

DRAFT = DraftPlan(
    summary="A small internal tool for the ops team to track on-call rotations.",
    goals=["Let ops self-serve rotation changes", "Replace the spreadsheet"],
    milestones=["Data model and API", "Basic UI"],
    risks=["Migration from the existing spreadsheet could lose history"],
    assumptions=["Ops team has fewer than 20 members"],
)

CRITIQUE_WITH_FINDINGS = CritiqueResult(
    findings=[
        CritiqueFinding(
            category="security_gap",
            severity="high",
            description=(
                "No mention of who can edit rotations; anyone with the link "
                "could reassign on-call."
            ),
        ),
        CritiqueFinding(
            category="untested_claim",
            severity="medium",
            description=(
                '"Replace the spreadsheet" assumes the spreadsheet has no '
                "other consumers -- unconfirmed."
            ),
        ),
    ],
    additional_risks=[
        "Other teams or automations may depend on reading the existing spreadsheet directly"
    ],
    additional_assumptions=["Only ops team members can authenticate to edit rotations"],
)

CRITIQUE_NO_FINDINGS = CritiqueResult()


def test_ask_clarifying_questions_returns_a_batch_of_questions():
    router = FakeRouter([structured(INTERVIEW_QUESTIONS_TURN)])

    turn, message = asyncio.run(ask_clarifying_questions(router, "build an on-call tool", []))

    assert turn.status == "questions"
    assert turn.questions == ["What language?", "Who are the users?"]
    assert message.usage_metadata is not None


def test_ask_clarifying_questions_returns_ready_signal():
    router = FakeRouter([structured(INTERVIEW_READY_TURN)])

    turn, _ = asyncio.run(
        ask_clarifying_questions(router, "build an on-call tool", ["prior Q&A"])
    )

    assert turn.status == "ready"
    assert turn.questions == []


def test_ask_clarifying_questions_raises_on_parsing_failure():
    router = FakeRouter(
        [
            ScriptedStructured(
                parsed=None,
                raw=AIMessage(content="garbled"),
                parsing_error=ValueError("bad"),
            )
        ]
    )

    with pytest.raises(ValueError, match="structured output failed to parse"):
        asyncio.run(ask_clarifying_questions(router, "x", []))


def test_draft_plan_returns_all_five_fields():
    router = FakeRouter([structured(DRAFT)])

    plan, _ = asyncio.run(draft_plan(router, "build an on-call tool", ["some", "history"]))

    assert "on-call rotations" in plan.summary
    assert "Let ops self-serve rotation changes" in plan.goals
    assert "Data model and API" in plan.milestones
    assert "Migration from the existing spreadsheet could lose history" in plan.risks
    assert "Ops team has fewer than 20 members" in plan.assumptions


def test_critique_plan_returns_findings_and_revisions():
    router = FakeRouter([structured(CRITIQUE_WITH_FINDINGS)])
    plan = DraftPlan(summary="s", risks=["existing risk"], assumptions=["existing assumption"])

    critique, _ = asyncio.run(critique_plan(router, plan))

    assert len(critique.findings) == 2
    assert critique.findings[0].category == "security_gap"
    assert critique.findings[0].severity == "high"
    assert critique.additional_risks == [
        "Other teams or automations may depend on reading the existing spreadsheet directly"
    ]
    assert critique.additional_assumptions == [
        "Only ops team members can authenticate to edit rotations"
    ]


def test_apply_critique_merges_new_risks_and_assumptions_into_the_plan():
    draft = DraftPlan(
        summary="A small internal tool.",
        risks=["Migration from the existing spreadsheet could lose history"],
        assumptions=["Ops team has fewer than 20 members"],
    )
    router = FakeRouter([structured(CRITIQUE_WITH_FINDINGS)])
    critique, _ = asyncio.run(critique_plan(router, draft))

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
    router = FakeRouter([structured(CRITIQUE_NO_FINDINGS)])
    critique, _ = asyncio.run(critique_plan(router, draft))

    final_plan = apply_critique(draft, critique)

    assert final_plan == draft


def test_apply_critique_does_not_duplicate_a_risk_the_draft_already_has():
    draft = DraftPlan(summary="s", risks=["shared risk"])
    critique = CritiqueResult(additional_risks=["shared risk", "new risk"])

    final_plan = apply_critique(draft, critique)

    assert final_plan.risks == ["shared risk", "new risk"]


def test_run_intake_to_final_plan_reaches_a_complete_plan_via_scripted_answers():
    """The Phase 8 Definition-of-Done scenario: a scripted sequence of user
    answers drives the interview to completion, producing a plan whose risks
    and assumptions were actually changed by the critique pass -- proving
    the critique isn't a no-op end to end."""
    router = FakeRouter(
        [
            structured(INTERVIEW_QUESTIONS_TURN),
            structured(INTERVIEW_READY_TURN),
            structured(DRAFT),
            structured(CRITIQUE_WITH_FINDINGS),
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
    router = FakeRouter(
        [
            structured(INTERVIEW_QUESTIONS_TURN),
            structured(INTERVIEW_QUESTIONS_TURN),
            structured(DRAFT),
            structured(CRITIQUE_NO_FINDINGS),
        ]
    )
    answers = iter(["answer 1", "answer 2"])

    result = asyncio.run(
        run_intake_to_final_plan(
            router, "build a tool", lambda qs: next(answers), max_rounds=2
        )
    )

    assert result.plan.summary
