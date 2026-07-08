"""Tests for cases_schema.load_cases -- fixture-file parsing and validation."""

import pytest
import yaml

from skills._eval_lib.cases_schema import EvalCase, InvalidCaseFileError, load_cases


def _write_yaml(path, data):
    path.write_text(yaml.safe_dump(data))


def test_load_cases_returns_eval_cases_in_file_order(tmp_path):
    cases_path = tmp_path / "cases.yaml"
    _write_yaml(
        cases_path,
        {
            "cases": [
                {"name": "first", "input": "a", "expect": {"must_match_any": ["x"]}},
                {"name": "second", "input": "b", "expect": {"must_match_any": ["y", "z"]}},
            ]
        },
    )

    cases = load_cases(cases_path)

    assert cases == [
        EvalCase(name="first", input="a", must_match_any=["x"], image_path=None),
        EvalCase(name="second", input="b", must_match_any=["y", "z"], image_path=None),
    ]


def test_load_cases_resolves_image_path_relative_to_cases_file(tmp_path):
    (tmp_path / "fixtures").mkdir()
    image_path = tmp_path / "fixtures" / "shot.png"
    image_path.write_bytes(b"fake-png-bytes")
    cases_path = tmp_path / "cases.yaml"
    _write_yaml(
        cases_path,
        {
            "cases": [
                {
                    "name": "has_image",
                    "input": "look at this",
                    "image": "fixtures/shot.png",
                    "expect": {"must_match_any": ["x"]},
                }
            ]
        },
    )

    cases = load_cases(cases_path)

    assert cases[0].image_path == image_path.resolve()


def test_load_cases_raises_on_missing_cases_key(tmp_path):
    cases_path = tmp_path / "cases.yaml"
    _write_yaml(cases_path, {"not_cases": []})

    with pytest.raises(InvalidCaseFileError, match="missing top-level 'cases:' key"):
        load_cases(cases_path)


def test_load_cases_raises_on_case_missing_required_key(tmp_path):
    cases_path = tmp_path / "cases.yaml"
    _write_yaml(cases_path, {"cases": [{"name": "no_input", "expect": {"must_match_any": ["x"]}}]})

    with pytest.raises(InvalidCaseFileError, match="missing keys"):
        load_cases(cases_path)


def test_load_cases_raises_on_empty_must_match_any(tmp_path):
    cases_path = tmp_path / "cases.yaml"
    _write_yaml(
        cases_path,
        {"cases": [{"name": "empty", "input": "a", "expect": {"must_match_any": []}}]},
    )

    with pytest.raises(InvalidCaseFileError, match="empty must_match_any"):
        load_cases(cases_path)


def test_load_cases_raises_on_missing_image_file(tmp_path):
    cases_path = tmp_path / "cases.yaml"
    _write_yaml(
        cases_path,
        {
            "cases": [
                {
                    "name": "bad_image",
                    "input": "a",
                    "image": "fixtures/does-not-exist.png",
                    "expect": {"must_match_any": ["x"]},
                }
            ]
        },
    )

    with pytest.raises(InvalidCaseFileError, match="missing image file"):
        load_cases(cases_path)
