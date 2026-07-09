"""Runs every eval case for one skill directory and reports pass/fail."""

import re
from pathlib import Path

from skills._eval_lib.cases_schema import load_cases
from skills._eval_lib.harness import CompleteFn, run_case

FRONTMATTER_PATTERN = re.compile(r"^---\n.*?\n---\n", re.DOTALL)


def load_skill_body(skill_md_path: Path) -> str:
    """Strips the YAML frontmatter from a SKILL.md file, returning its body.

    Args:
        skill_md_path: Path to a SKILL.md file.

    Returns:
        The file's content with the leading `---`-delimited frontmatter
        block removed.
    """
    text = skill_md_path.read_text()
    return FRONTMATTER_PATTERN.sub("", text, count=1)


def run_skill_eval(skill_dir: Path, complete_fn: CompleteFn) -> int:
    """Runs every case in <skill_dir>/eval/cases.yaml against <skill_dir>/SKILL.md.

    Args:
        skill_dir: A skill's root directory (e.g. skills/review-code).
        complete_fn: A (system_prompt, user_prompt, image_bytes) -> completion_text
            callable.

    Returns:
        0 if every case passed, 1 if any case failed.
    """
    skill_body = load_skill_body(skill_dir / "SKILL.md")
    cases = load_cases(skill_dir / "eval" / "cases.yaml")
    exit_code = 0
    for case in cases:
        result = run_case(complete_fn, skill_body, case)
        status = "PASS" if result.passed else "FAIL"
        print(f"[{status}] {result.case_name}")
        if not result.passed:
            exit_code = 1
    return exit_code
