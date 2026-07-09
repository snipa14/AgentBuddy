"""Tests for cli.run_skill_eval -- wires cases_schema + harness for one skill dir."""

from skills._eval_lib.cli import load_skill_body, run_skill_eval


def _make_skill(tmp_path, skill_md_body, cases_yaml_text):
    skill_dir = tmp_path / "some-skill"
    (skill_dir / "eval").mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: some-skill\ndescription: does a thing\n---\n{skill_md_body}"
    )
    (skill_dir / "eval" / "cases.yaml").write_text(cases_yaml_text)
    return skill_dir


def test_load_skill_body_strips_frontmatter(tmp_path):
    skill_md = tmp_path / "SKILL.md"
    skill_md.write_text("---\nname: x\ndescription: y\n---\n# Body\n\nContent here.\n")

    assert load_skill_body(skill_md) == "# Body\n\nContent here.\n"


def test_run_skill_eval_returns_zero_when_all_cases_pass(tmp_path):
    skill_dir = _make_skill(
        tmp_path,
        "# Some Skill\n\nFlag hardcoded secrets.\n",
        "cases:\n  - name: c1\n    input: review this\n    expect:\n      must_match_any: [x]\n",
    )

    def fake_complete(system_prompt, user_prompt, image_bytes):
        return "found x here"

    assert run_skill_eval(skill_dir, fake_complete) == 0


def test_run_skill_eval_returns_one_when_any_case_fails(tmp_path):
    skill_dir = _make_skill(
        tmp_path,
        "# Some Skill\n",
        "cases:\n"
        "  - name: passes\n    input: a\n    expect:\n      must_match_any: [x]\n"
        "  - name: fails\n    input: b\n    expect:\n      must_match_any: [never_present]\n",
    )

    def fake_complete(system_prompt, user_prompt, image_bytes):
        return "contains x"

    assert run_skill_eval(skill_dir, fake_complete) == 1


def test_run_skill_eval_passes_skill_body_not_frontmatter_to_complete_fn(tmp_path):
    skill_dir = _make_skill(
        tmp_path,
        "# Real Body\n",
        "cases:\n  - name: c1\n    input: hi\n    expect:\n      must_match_any: [x]\n",
    )
    captured = {}

    def fake_complete(system_prompt, user_prompt, image_bytes):
        captured["system_prompt"] = system_prompt
        return "x"

    run_skill_eval(skill_dir, fake_complete)

    assert "name: some-skill" not in captured["system_prompt"]
    assert captured["system_prompt"] == "# Real Body\n"
