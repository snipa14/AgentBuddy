"""Planner / Intake agent (ARCHITECTURE.md §4.7, §6.1; skills/roast-the-plan/SKILL.md).

Four atomic LLM-backed steps -- ask_clarifying_questions, draft_plan,
critique_plan -- plus one pure merge step, apply_critique. Each is a single
request/response with no internal looping, so a LangGraph node built on top
of one of them can safely call `interrupt()` right after it without
replaying an LLM call on resume (see services/orchestrator/graph/nodes.py's
module docstring for why that split matters).

Every LLM-backed function returns `(result, ai_message)` -- the raw message
carries `usage_metadata` for the calling node to price via
`ModelRouter.cost_usd()` (ADR-025/026); this module itself never reasons
about pricing.

`run_intake_to_final_plan` is a Python-loop reference implementation of the
whole flow, used to test this module's logic end to end with a scripted
sequence of answers. It is deliberately NOT what the real graph nodes call:
the graph drives the same round-by-round shape via checkpointed self-loop
edges instead of a single node looping internally, because -- per
LangGraph's own interrupt() semantics -- a node that calls an LLM before
`interrupt()` would re-run that LLM call on every resume.
"""

from typing import Callable, Literal

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from pydantic import BaseModel

from services.orchestrator.agents.llm import invoke_structured
from services.orchestrator.model_router.router import ModelRouter
from skills.loader import load_skill

PLANNER_ROLE = "planner"

CritiqueCategory = Literal[
    "unstated_assumption",
    "missing_edge_case",
    "security_gap",
    "scope_creep",
    "untested_claim",
]
Severity = Literal["low", "medium", "high"]


class InterviewTurn(BaseModel):
    """One round's worth of Planner output during the clarifying-question interview."""

    status: Literal["questions", "ready"]
    questions: list[str] = []


class DraftPlan(BaseModel):
    """A project plan, before or after the critique pass."""

    summary: str
    goals: list[str] = []
    milestones: list[str] = []
    risks: list[str] = []
    assumptions: list[str] = []


class CritiqueFinding(BaseModel):
    """One finding from the roast-the-plan critique pass."""

    category: CritiqueCategory
    severity: Severity
    description: str


class CritiqueResult(BaseModel):
    """The critique pass's full output: findings plus concrete plan revisions."""

    findings: list[CritiqueFinding] = []
    additional_risks: list[str] = []
    additional_assumptions: list[str] = []


class IntakeResult(BaseModel):
    """The full intake-to-approval flow's output."""

    plan: DraftPlan
    critique_findings: list[CritiqueFinding]


_INTERVIEW_SYSTEM_PROMPT = (
    "You are interviewing a stakeholder to plan a new software project. Ask "
    "clarifying questions in small batches (at most 3 at a time) until you "
    "have enough to draft a plan covering goals, milestones, risks, and "
    "assumptions."
)

_DRAFT_SYSTEM_PROMPT = (
    "You are drafting a software project plan from an interview transcript. "
    "Produce a summary, goals, milestones, risks, and assumptions (the last "
    "four may be empty lists)."
)


def _format_transcript(qa_history: list[str]) -> str:
    lines = []
    for index, entry in enumerate(qa_history):
        speaker = "Assistant" if index % 2 == 0 else "Stakeholder"
        lines.append(f"{speaker}: {entry}")
    return "\n".join(lines)


async def ask_clarifying_questions(
    router: ModelRouter, project_description: str, qa_history: list[str]
) -> tuple[InterviewTurn, AIMessage]:
    """Runs one round of the clarifying-question interview.

    Args:
        router: A ModelRouter to resolve the "planner" role from.
        project_description: The stakeholder's initial project request.
        qa_history: The interview transcript so far, alternating assistant
            questions and stakeholder answers (assistant entries at even
            indices).

    Returns:
        A tuple of (InterviewTurn, raw AIMessage) -- either a fresh batch of
        questions or a signal that enough information has been gathered to
        draft a plan, plus the raw message for cost tracking.

    Raises:
        NoCapableProviderError: If no provider is configured for "planner".
        ValueError: If the model's response doesn't match InterviewTurn.
    """
    llm = router.resolve(PLANNER_ROLE)
    messages = [
        SystemMessage(content=_INTERVIEW_SYSTEM_PROMPT),
        HumanMessage(
            content=f"Project request: {project_description}\n\nTranscript so far:\n"
            f"{_format_transcript(qa_history)}"
        ),
    ]
    return await invoke_structured(llm, InterviewTurn, messages)


async def draft_plan(
    router: ModelRouter, project_description: str, qa_history: list[str]
) -> tuple[DraftPlan, AIMessage]:
    """Drafts a project plan from a completed interview transcript.

    Args:
        router: A ModelRouter to resolve the "planner" role from.
        project_description: The stakeholder's initial project request.
        qa_history: The full interview transcript.

    Returns:
        A tuple of (DraftPlan, raw AIMessage), before critique.

    Raises:
        NoCapableProviderError: If no provider is configured for "planner".
        ValueError: If the model's response doesn't match DraftPlan.
    """
    llm = router.resolve(PLANNER_ROLE)
    messages = [
        SystemMessage(content=_DRAFT_SYSTEM_PROMPT),
        HumanMessage(
            content=f"Project request: {project_description}\n\nInterview transcript:\n"
            f"{_format_transcript(qa_history)}"
        ),
    ]
    return await invoke_structured(llm, DraftPlan, messages)


async def critique_plan(router: ModelRouter, plan: DraftPlan) -> tuple[CritiqueResult, AIMessage]:
    """Runs the roast-the-plan adversarial critique pass over a draft plan.

    Args:
        router: A ModelRouter to resolve the "planner" role from.
        plan: The draft plan to critique.

    Returns:
        A tuple of (CritiqueResult, raw AIMessage) with findings and any
        concrete plan revisions.

    Raises:
        NoCapableProviderError: If no provider is configured for "planner".
        ValueError: If the model's response doesn't match CritiqueResult.
    """
    llm = router.resolve(PLANNER_ROLE)
    plan_text = (
        f"Summary: {plan.summary}\n"
        f"Goals: {plan.goals}\n"
        f"Milestones: {plan.milestones}\n"
        f"Risks: {plan.risks}\n"
        f"Assumptions: {plan.assumptions}"
    )
    messages = [
        SystemMessage(content=load_skill("roast-the-plan").body),
        HumanMessage(content=plan_text),
    ]
    return await invoke_structured(llm, CritiqueResult, messages)


def apply_critique(plan: DraftPlan, critique: CritiqueResult) -> DraftPlan:
    """Merges a critique's plan revisions into a draft plan.

    Args:
        plan: The pre-critique draft plan.
        critique: The critique pass's output.

    Returns:
        A new DraftPlan with any additional risks/assumptions from the
        critique appended (duplicates against the original skipped).
    """
    merged_risks = plan.risks + [
        risk for risk in critique.additional_risks if risk not in plan.risks
    ]
    merged_assumptions = plan.assumptions + [
        assumption
        for assumption in critique.additional_assumptions
        if assumption not in plan.assumptions
    ]
    return plan.model_copy(update={"risks": merged_risks, "assumptions": merged_assumptions})


async def run_intake_to_final_plan(
    router: ModelRouter,
    project_description: str,
    answer_fn: Callable[[list[str]], str],
    max_rounds: int = 5,
) -> IntakeResult:
    """Reference implementation of the full intake-to-approval flow.

    Loops the clarifying-question interview until the Planner signals it's
    ready (or max_rounds is reached), then drafts and critiques the plan.
    See this module's docstring for why the real LangGraph nodes don't call
    this function directly.

    Args:
        router: A ModelRouter to resolve the "planner" role from.
        project_description: The stakeholder's initial project request.
        answer_fn: Called with each round's questions, returning the
            stakeholder's answer as a single string.
        max_rounds: Safety cap on interview rounds.

    Returns:
        An IntakeResult with the final (post-critique) plan and the raw
        critique findings.
    """
    qa_history: list[str] = []
    for _ in range(max_rounds):
        turn, _ = await ask_clarifying_questions(router, project_description, qa_history)
        if turn.status == "ready":
            break
        qa_history.append("\n".join(turn.questions))
        qa_history.append(answer_fn(turn.questions))

    draft, _ = await draft_plan(router, project_description, qa_history)
    critique, _ = await critique_plan(router, draft)
    final_plan = apply_critique(draft, critique)
    return IntakeResult(plan=final_plan, critique_findings=critique.findings)
