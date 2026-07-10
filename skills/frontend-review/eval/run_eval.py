# skills/frontend-review/eval/run_eval.py
"""Runs this skill's eval cases against a real, configured model."""

import sys
from pathlib import Path

from skills._eval_lib.cli import run_skill_eval
from skills._eval_lib.models import default_complete_fn

if __name__ == "__main__":
    sys.exit(run_skill_eval(Path(__file__).resolve().parent.parent, default_complete_fn()))
