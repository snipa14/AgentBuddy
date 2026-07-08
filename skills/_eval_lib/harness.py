"""Pure evaluation logic for skill eval cases -- no network, fully unit-testable."""

import re
from collections.abc import Callable
from dataclasses import dataclass

from skills._eval_lib.cases_schema import EvalCase

CompleteFn = Callable[[str, str, bytes | None], str]
"""A (system_prompt, user_prompt, image_bytes) -> completion_text callable.
image_bytes is None for text-only cases."""


@dataclass(frozen=True)
class EvalResult:
    """The outcome of running one EvalCase against a skill.

    Attributes:
        case_name: The EvalCase's name, echoed through for reporting.
        passed: True if any of the case's must_match_any patterns matched.
        output: The raw completion text produced by complete_fn.
        matched_pattern: The first pattern that matched, or None if none did.
    """

    case_name: str
    passed: bool
    output: str
    matched_pattern: str | None


def run_case(complete_fn: CompleteFn, skill_body: str, case: EvalCase) -> EvalResult:
    """Runs one eval case against a skill by calling complete_fn once.

    Args:
        complete_fn: A (system_prompt, user_prompt, image_bytes) -> completion_text
            callable.
        skill_body: The skill's SKILL.md body (everything after the frontmatter),
            sent verbatim as the system prompt.
        case: The EvalCase to run.

    Returns:
        An EvalResult recording whether any of the case's must_match_any
        patterns were found (case-insensitively) in the completion.
    """
    image_bytes = case.image_path.read_bytes() if case.image_path else None
    output = complete_fn(skill_body, case.input, image_bytes)
    for pattern in case.must_match_any:
        if re.search(pattern, output, re.IGNORECASE):
            return EvalResult(case.name, True, output, pattern)
    return EvalResult(case.name, False, output, None)
