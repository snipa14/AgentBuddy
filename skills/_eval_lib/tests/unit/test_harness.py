"""Tests for harness.run_case -- pure eval-case execution, no network."""

from skills._eval_lib.cases_schema import EvalCase
from skills._eval_lib.harness import run_case


def test_run_case_passes_when_a_pattern_matches_case_insensitively():
    def fake_complete(system_prompt, user_prompt, image_bytes):
        return "Found a HARDCODED credential in the diff."

    case = EvalCase(name="flags_secret", input="review this", must_match_any=["hardcod", "secret"])

    result = run_case(fake_complete, "system prompt body", case)

    assert result.passed is True
    assert result.case_name == "flags_secret"
    assert result.matched_pattern == "hardcod"


def test_run_case_fails_when_no_pattern_matches():
    def fake_complete(system_prompt, user_prompt, image_bytes):
        return "This diff looks fine."

    case = EvalCase(name="flags_secret", input="review this", must_match_any=["hardcod", "secret"])

    result = run_case(fake_complete, "system prompt body", case)

    assert result.passed is False
    assert result.matched_pattern is None
    assert result.output == "This diff looks fine."


def test_run_case_passes_skill_body_and_input_to_complete_fn():
    captured = {}

    def fake_complete(system_prompt, user_prompt, image_bytes):
        captured["system_prompt"] = system_prompt
        captured["user_prompt"] = user_prompt
        captured["image_bytes"] = image_bytes
        return "irrelevant"

    case = EvalCase(name="c", input="the user prompt", must_match_any=["x"])
    run_case(fake_complete, "the skill body", case)

    assert captured == {
        "system_prompt": "the skill body",
        "user_prompt": "the user prompt",
        "image_bytes": None,
    }


def test_run_case_reads_and_forwards_image_bytes_when_case_has_an_image(tmp_path):
    image_path = tmp_path / "shot.png"
    image_path.write_bytes(b"\x89PNG-fake-bytes")
    captured = {}

    def fake_complete(system_prompt, user_prompt, image_bytes):
        captured["image_bytes"] = image_bytes
        return "irrelevant"

    case = EvalCase(name="c", input="look", must_match_any=["x"], image_path=image_path)
    run_case(fake_complete, "body", case)

    assert captured["image_bytes"] == b"\x89PNG-fake-bytes"
