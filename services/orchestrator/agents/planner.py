"""Planner / Intake agent (ARCHITECTURE.md §4.7, §6.1; skills/roast-the-plan/SKILL.md).

Four atomic LLM-backed steps -- ask_clarifying_questions, draft_plan,
critique_plan -- plus one pure merge step, apply_critique. Each is a single
request/response with no internal looping, so a LangGraph node built on top
of one of them can safely call `interrupt()` right after it without
replaying an LLM call on resume (see services/orchestrator/graph/nodes.py's
module docstring for why that split matters).

`run_intake_to_final_plan` is a Python-loop reference implementation of the
whole flow, used to test this module's logic end to end with a scripted
sequence of answers. It is deliberately NOT what the real graph nodes call:
the graph drives the same round-by-round shape via checkpointed self-loop
edges instead of a single node looping internally, because -- per
LangGraph's own interrupt() semantics -- a node that calls an LLM before
`interrupt()` would re-run that LLM call on every resume.
"""

from typing import Callable, Literal

from pydantic import BaseModel

from services.orchestrator.model_router.router import ModelRouter
from services.orchestrator.model_router.schemas import CompletionMessage, CompletionRequest

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
    "assumptions. Respond with exactly one of two formats. If you need more "
    "information: '## Status\\nquestions\\n\\n## Questions\\n' followed by "
    "one question per line, each starting with '- '. If you have enough: "
    "'## Status\\nready\\n'."
)

_DRAFT_SYSTEM_PROMPT = (
    "You are drafting a software project plan from an interview transcript. "
    "Respond with exactly these five markdown sections, in order: "
    "'## Summary' (a short paragraph), '## Goals', '## Milestones', "
    "'## Risks', '## Assumptions' (the last four as '- ' bullet lists, "
    "which may be empty)."
)

_CRITIQUE_SYSTEM_PROMPT = (
    "You are the roast-the-plan critique pass (see skills/roast-the-plan/"
    "SKILL.md): adversarially review the given project plan for unstated "
    "assumptions, missing edge cases, security gaps, scope creep, and "
    "untested claims. Respond with '## Findings' followed by zero or more "
    "lines formatted as 'category: <unstated_assumption|missing_edge_case|"
    "security_gap|scope_creep|untested_claim> | severity: <low|medium|high> "
    "| <description>', then '## Additional Risks' and '## Additional "
    "Assumptions', each as '- ' bullet lists (which may be empty) of new "
    "risks/assumptions the plan should carry as a result of your findings."
)


def _format_transcript(qa_history: list[str]) -> str:
    lines = []
    for index, entry in enumerate(qa_history):
        speaker = "Assistant" if index % 2 == 0 else "Stakeholder"
        lines.append(f"{speaker}: {entry}")
    return "\n".join(lines)


async def _complete(router: ModelRouter, system_prompt: str, user_content: str) -> str:
    provider = router.resolve(PLANNER_ROLE)
    request = CompletionRequest(
        messages=[
            CompletionMessage(role="system", content=system_prompt),
            CompletionMessage(role="user", content=user_content),
        ]
    )
    result = await provider.complete(request)
    return result.content


async def ask_clarifying_questions(
    router: ModelRouter, project_description: str, qa_history: list[str]
) -> InterviewTurn:
    """Runs one round of the clarifying-question interview.

    Args:
        router: A ModelRouter to resolve the "planner" role from.
        project_description: The stakeholder's initial project request.
        qa_history: The interview transcript so far, alternating assistant
            questions and stakeholder answers (assistant entries at even
            indices).

    Returns:
        An InterviewTurn: either a fresh batch of questions, or a signal
        that enough information has been gathered to draft a plan.

    Raises:
        NoCapableProviderError: If no provider is configured for "planner".
        ProviderRequestError: If the underlying model call fails.
        ValueError: If the model's response doesn't match the expected format.
    """
    content = await _complete(
        router,
        _INTERVIEW_SYSTEM_PROMPT,
        f"Project request: {project_description}\n\nTranscript so far:\n"
        f"{_format_transcript(qa_history)}",
    )
    return _parse_interview_turn(content)


async def draft_plan(
    router: ModelRouter, project_description: str, qa_history: list[str]
) -> DraftPlan:
    """Drafts a project plan from a completed interview transcript.

    Args:
        router: A ModelRouter to resolve the "planner" role from.
        project_description: The stakeholder's initial project request.
        qa_history: The full interview transcript.

    Returns:
        A DraftPlan, before critique.

    Raises:
        NoCapableProviderError: If no provider is configured for "planner".
        ProviderRequestError: If the underlying model call fails.
        ValueError: If the model's response doesn't match the expected format.
    """
    content = await _complete(
        router,
        _DRAFT_SYSTEM_PROMPT,
        f"Project request: {project_description}\n\nInterview transcript:\n"
        f"{_format_transcript(qa_history)}",
    )
    return _parse_draft_plan(content)


async def critique_plan(router: ModelRouter, plan: DraftPlan) -> CritiqueResult:
    """Runs the roast-the-plan adversarial critique pass over a draft plan.

    Args:
        router: A ModelRouter to resolve the "planner" role from.
        plan: The draft plan to critique.

    Returns:
        A CritiqueResult with findings and any concrete plan revisions.

    Raises:
        NoCapableProviderError: If no provider is configured for "planner".
        ProviderRequestError: If the underlying model call fails.
        ValueError: If the model's response doesn't match the expected format.
    """
    plan_text = (
        f"Summary: {plan.summary}\n"
        f"Goals: {plan.goals}\n"
        f"Milestones: {plan.milestones}\n"
        f"Risks: {plan.risks}\n"
        f"Assumptions: {plan.assumptions}"
    )
    content = await _complete(router, _CRITIQUE_SYSTEM_PROMPT, plan_text)
    return _parse_critique_result(content)


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
        turn = await ask_clarifying_questions(router, project_description, qa_history)
        if turn.status == "ready":
            break
        qa_history.append("\n".join(turn.questions))
        qa_history.append(answer_fn(turn.questions))

    draft = await draft_plan(router, project_description, qa_history)
    critique = await critique_plan(router, draft)
    final_plan = apply_critique(draft, critique)
    return IntakeResult(plan=final_plan, critique_findings=critique.findings)


def _parse_interview_turn(content: str) -> InterviewTurn:
    """Parses an InterviewTurn from a '## Status' / '## Questions' response.

    Raises:
        ValueError: If the status line is missing or isn't "questions"/"ready".
    """
    sections = _split_sections(content)
    status = sections.get("Status", "").strip()
    if status not in ("questions", "ready"):
        raise ValueError(f"malformed interview status: {content!r}")
    questions = _bullet_lines(sections.get("Questions", "")) if status == "questions" else []
    return InterviewTurn(status=status, questions=questions)


def _parse_draft_plan(content: str) -> DraftPlan:
    """Parses a DraftPlan from the five expected markdown sections.

    Raises:
        ValueError: If the '## Summary' section is missing.
    """
    sections = _split_sections(content)
    summary = sections.get("Summary", "").strip()
    if not summary:
        raise ValueError(f"malformed draft plan, missing Summary section: {content!r}")
    return DraftPlan(
        summary=summary,
        goals=_bullet_lines(sections.get("Goals", "")),
        milestones=_bullet_lines(sections.get("Milestones", "")),
        risks=_bullet_lines(sections.get("Risks", "")),
        assumptions=_bullet_lines(sections.get("Assumptions", "")),
    )


def _parse_critique_result(content: str) -> CritiqueResult:
    """Parses a CritiqueResult from '## Findings'/'## Additional Risks'/'## Additional Assumptions'.

    Raises:
        ValueError: If a findings line doesn't match the expected pipe-delimited format.
    """
    sections = _split_sections(content)
    findings = [
        _parse_finding_line(line) for line in _nonempty_lines(sections.get("Findings", ""))
    ]
    return CritiqueResult(
        findings=findings,
        additional_risks=_bullet_lines(sections.get("Additional Risks", "")),
        additional_assumptions=_bullet_lines(sections.get("Additional Assumptions", "")),
    )


def _parse_finding_line(line: str) -> CritiqueFinding:
    """Parses one 'category: X | severity: Y | description' line.

    Raises:
        ValueError: If the line doesn't match the expected pipe-delimited format.
    """
    parts = [part.strip() for part in line.split("|", maxsplit=2)]
    if (
        len(parts) != 3
        or not parts[0].startswith("category:")
        or not parts[1].startswith("severity:")
    ):
        raise ValueError(f"malformed critique finding line: {line!r}")
    return CritiqueFinding(
        category=parts[0].removeprefix("category:").strip(),
        severity=parts[1].removeprefix("severity:").strip(),
        description=parts[2].strip(),
    )


def _split_sections(content: str) -> dict[str, str]:
    """Splits a '## Heading' delimited response into {heading: body} pairs."""
    sections: dict[str, str] = {}
    current_heading: str | None = None
    current_lines: list[str] = []
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            if current_heading is not None:
                sections[current_heading] = "\n".join(current_lines)
            current_heading = stripped.removeprefix("## ").strip()
            current_lines = []
        elif current_heading is not None:
            current_lines.append(line)
    if current_heading is not None:
        sections[current_heading] = "\n".join(current_lines)
    return sections


def _bullet_lines(section_body: str) -> list[str]:
    """Extracts '- ' bullet list items from a section's raw text body."""
    items = []
    for line in section_body.splitlines():
        stripped = line.strip()
        if stripped.startswith("- "):
            items.append(stripped.removeprefix("- ").strip())
    return items


def _nonempty_lines(section_body: str) -> list[str]:
    """Extracts non-empty, non-bulleted lines from a section's raw text body."""
    return [line.strip() for line in section_body.splitlines() if line.strip()]
