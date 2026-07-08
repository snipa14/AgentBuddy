# Phase 19: Skills Packaging Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every skill in `ARCHITECTURE.md` §4.8 exists as a portable `SKILL.md` with at least 2 passing eval cases, verified through a standalone eval harness that is independent of Foundry's `ModelRouter`/`config/models.yaml`.

**Architecture:** A shared, pure-logic eval harness (`skills/_eval_lib/`) with an injectable `complete_fn`, so its parsing/matching logic gets real pytest coverage with a fake completion function while the actual network-calling code (`models.py`) is thin, isolated, and reviewed by hand (no `ANTHROPIC_API_KEY`/Ollama available in this sandbox). Each skill gets `SKILL.md` + `eval/cases.yaml` (>=2 fixture cases) + `eval/run_eval.py` (a 6-line entrypoint calling the shared harness).

**Tech Stack:** Python 3.12 stdlib + `pyyaml` (already a dependency) + `httpx` (already a dependency, used directly for both Anthropic and Ollama HTTP calls — no LangChain dependency in this harness, keeping it usable outside Foundry entirely, per ADR-014).

## Global Constraints

- `complete_fn` signature is exactly `Callable[[str, str, bytes | None], str]` — `(system_prompt, user_prompt, image_bytes) -> completion_text`. `image_bytes` is `None` for every skill except `frontend-review`.
- `must_match_any` checks are case-insensitive regex/substring matches against the *category* of a finding, never exact wording (per `.claude/rules/testing.md`'s explicit warning against flaky content-exact tests).
- No `SKILL.md` may contain a provider-specific instruction (no model names, no "as Claude..." phrasing, no provider-specific formatting assumptions) — verified by inspection in each task's self-review, not by dual-provider execution.
- The eval harness does not import or depend on `services.orchestrator.model_router` or `config/models.yaml` in any way.
- `pyproject.toml`'s `testpaths = ["services"]` means `skills/_eval_lib/tests/` is never picked up by a bare `pytest` invocation — always run it with an explicit path: `pytest skills/_eval_lib/tests/unit`.
- No `ANTHROPIC_API_KEY` and no running Ollama instance are available in this development sandbox. `models.py`'s actual HTTP request/response handling cannot be exercised for real here — only its provider-*selection* logic is unit-tested (with the HTTP-calling functions patched out). This mirrors the `langgraph`-missing gap from Phase 18: state it plainly in commit/report text, never claim an execution that didn't happen.
- Every new Python file gets a Google-style docstring on each public function/class, full type hints (no bare `Any`), and `snake_case`/`PascalCase`/`SCREAMING_SNAKE_CASE` naming per `CLAUDE.md`.

---

### Task 1: `cases_schema.py` — load and validate `cases.yaml`

**Files:**
- Create: `skills/__init__.py`
- Create: `skills/_eval_lib/__init__.py`
- Create: `skills/_eval_lib/cases_schema.py`
- Test: `skills/_eval_lib/tests/unit/test_cases_schema.py`
- Test: `skills/_eval_lib/tests/unit/__init__.py`
- Test: `skills/_eval_lib/tests/__init__.py`

**Interfaces:**
- Consumes: nothing (first task).
- Produces: `EvalCase` (frozen dataclass: `name: str`, `input: str`, `must_match_any: list[str]`, `image_path: Path | None`), `InvalidCaseFileError(ValueError)`, `load_cases(cases_path: Path) -> list[EvalCase]`. Later tasks (2, 4, 5-11) import all three from `skills._eval_lib.cases_schema`.

- [ ] **Step 1: Write the failing tests**

```python
# skills/_eval_lib/tests/unit/test_cases_schema.py
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
```

```python
# skills/_eval_lib/tests/unit/__init__.py
```

```python
# skills/_eval_lib/tests/__init__.py
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest skills/_eval_lib/tests/unit/test_cases_schema.py -v`
Expected: `ModuleNotFoundError: No module named 'skills._eval_lib.cases_schema'` (or `skills._eval_lib` itself, since neither exists yet).

- [ ] **Step 3: Write `skills/__init__.py` and `skills/_eval_lib/__init__.py`**

```python
# skills/__init__.py
"""Portable Agent Skills (SKILL.md + eval fixtures) for Foundry's agent roles."""
```

```python
# skills/_eval_lib/__init__.py
"""Shared, standalone eval harness for verifying skills/*/SKILL.md files.

Deliberately independent of services.orchestrator.model_router and
config/models.yaml -- per ADR-014, skills must work identically whether the
executing model is local, Claude API, or a harness outside Foundry
entirely, so their evals must not require Foundry's own routing config.
"""
```

- [ ] **Step 4: Write `cases_schema.py`**

```python
# skills/_eval_lib/cases_schema.py
"""Loads and validates skill eval fixture cases from a cases.yaml file."""

from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class EvalCase:
    """One fixture case for a skill's eval: an input plus what must show up in the response.

    Attributes:
        name: A short, unique identifier for this case (used in PASS/FAIL output).
        input: The user-prompt text sent to the model alongside the skill's body.
        must_match_any: Case-insensitive regex patterns; the case passes if the
            model's response matches at least one.
        image_path: Resolved absolute path to an image file to send alongside
            `input`, or None for a text-only case.
    """

    name: str
    input: str
    must_match_any: list[str]
    image_path: Path | None = None


class InvalidCaseFileError(ValueError):
    """Raised when a cases.yaml file is missing required fields or malformed."""


def load_cases(cases_path: Path) -> list[EvalCase]:
    """Loads and validates eval cases from a cases.yaml file.

    Args:
        cases_path: Path to a skill's eval/cases.yaml file.

    Returns:
        The EvalCase objects declared under the file's `cases:` key, in file
        order. An `image` key, if present on a case, is resolved relative to
        cases_path's parent directory.

    Raises:
        InvalidCaseFileError: If the file has no `cases:` key, a case is
            missing `name`/`input`/`expect`, `expect.must_match_any` is
            empty, or a referenced `image` file doesn't exist.
    """
    raw = yaml.safe_load(cases_path.read_text())
    if not raw or "cases" not in raw:
        raise InvalidCaseFileError(f"{cases_path}: missing top-level 'cases:' key")

    cases: list[EvalCase] = []
    for i, entry in enumerate(raw["cases"]):
        missing = [key for key in ("name", "input", "expect") if key not in entry]
        if missing:
            raise InvalidCaseFileError(f"{cases_path}: case {i} missing keys {missing}")

        must_match_any = entry["expect"].get("must_match_any")
        if not must_match_any:
            raise InvalidCaseFileError(
                f"{cases_path}: case {i} ('{entry['name']}') has an empty must_match_any"
            )

        image_path = None
        if "image" in entry:
            image_path = (cases_path.parent / entry["image"]).resolve()
            if not image_path.is_file():
                raise InvalidCaseFileError(
                    f"{cases_path}: case {i} ('{entry['name']}') references "
                    f"missing image file {image_path}"
                )

        cases.append(
            EvalCase(
                name=entry["name"],
                input=entry["input"],
                must_match_any=must_match_any,
                image_path=image_path,
            )
        )
    return cases
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest skills/_eval_lib/tests/unit/test_cases_schema.py -v`
Expected: `6 passed`

- [ ] **Step 6: Commit**

```bash
git add skills/__init__.py skills/_eval_lib/__init__.py skills/_eval_lib/cases_schema.py \
        skills/_eval_lib/tests/__init__.py skills/_eval_lib/tests/unit/__init__.py \
        skills/_eval_lib/tests/unit/test_cases_schema.py
git commit -m "feat(skills): add cases.yaml schema loader for skill evals"
```

---

### Task 2: `harness.py` — run one eval case

**Files:**
- Create: `skills/_eval_lib/harness.py`
- Test: `skills/_eval_lib/tests/unit/test_harness.py`

**Interfaces:**
- Consumes: `EvalCase` from `skills._eval_lib.cases_schema` (Task 1).
- Produces: `CompleteFn = Callable[[str, str, bytes | None], str]`, `EvalResult` (frozen dataclass: `case_name: str`, `passed: bool`, `output: str`, `matched_pattern: str | None`), `run_case(complete_fn: CompleteFn, skill_body: str, case: EvalCase) -> EvalResult`. Tasks 3 and 4 import `CompleteFn`; Task 4 imports `run_case`.

- [ ] **Step 1: Write the failing tests**

```python
# skills/_eval_lib/tests/unit/test_harness.py
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest skills/_eval_lib/tests/unit/test_harness.py -v`
Expected: `ModuleNotFoundError: No module named 'skills._eval_lib.harness'`

- [ ] **Step 3: Write `harness.py`**

```python
# skills/_eval_lib/harness.py
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest skills/_eval_lib/tests/unit/test_harness.py -v`
Expected: `4 passed`

- [ ] **Step 5: Commit**

```bash
git add skills/_eval_lib/harness.py skills/_eval_lib/tests/unit/test_harness.py
git commit -m "feat(skills): add pure eval-case runner (harness.run_case)"
```

---

### Task 3: `models.py` — real completion function from environment

**Files:**
- Create: `skills/_eval_lib/models.py`
- Test: `skills/_eval_lib/tests/unit/test_models.py`

**Interfaces:**
- Consumes: `CompleteFn` from `skills._eval_lib.harness` (Task 2).
- Produces: `NoProviderConfiguredError(RuntimeError)`, `default_complete_fn() -> CompleteFn`. Task 4's per-skill `run_eval.py` files import `default_complete_fn`.

- [ ] **Step 1: Write the failing tests**

Only the provider-*selection* logic is tested here — the actual HTTP request/response handling inside `_anthropic_complete`/`_ollama_complete` needs a real `ANTHROPIC_API_KEY` or Ollama instance to exercise meaningfully, neither of which exists in this sandbox (Global Constraints). The factory functions are patched out so the test proves *which one gets called*, not what it does.

```python
# skills/_eval_lib/tests/unit/test_models.py
"""Tests for models.default_complete_fn -- provider-selection logic only.

_anthropic_complete/_ollama_complete's actual HTTP behavior needs a real
ANTHROPIC_API_KEY or Ollama instance to test meaningfully; neither is
available in this sandbox, so they're patched out here and reviewed by hand
instead (see the module docstring).
"""

import pytest

from skills._eval_lib import models


def test_default_complete_fn_prefers_anthropic_when_api_key_set(monkeypatch):
    monkeypatch.setenv(models.ANTHROPIC_API_KEY_ENV_VAR, "sk-ant-fake")
    monkeypatch.setenv(models.OLLAMA_BASE_URL_ENV_VAR, "http://ollama:11434")
    monkeypatch.setattr(models, "_anthropic_complete", lambda key, model: "anthropic-marker")
    monkeypatch.setattr(models, "_ollama_complete", lambda url, model: "ollama-marker")

    assert models.default_complete_fn() == "anthropic-marker"


def test_default_complete_fn_falls_back_to_ollama_when_no_api_key(monkeypatch):
    monkeypatch.delenv(models.ANTHROPIC_API_KEY_ENV_VAR, raising=False)
    monkeypatch.setenv(models.OLLAMA_BASE_URL_ENV_VAR, "http://ollama:11434")
    monkeypatch.setattr(models, "_ollama_complete", lambda url, model: "ollama-marker")

    assert models.default_complete_fn() == "ollama-marker"


def test_default_complete_fn_raises_when_neither_configured(monkeypatch):
    monkeypatch.delenv(models.ANTHROPIC_API_KEY_ENV_VAR, raising=False)
    monkeypatch.delenv(models.OLLAMA_BASE_URL_ENV_VAR, raising=False)

    with pytest.raises(models.NoProviderConfiguredError):
        models.default_complete_fn()


def test_default_complete_fn_uses_model_env_var_override(monkeypatch):
    monkeypatch.setenv(models.ANTHROPIC_API_KEY_ENV_VAR, "sk-ant-fake")
    monkeypatch.setenv(models.ANTHROPIC_MODEL_ENV_VAR, "claude-opus-4-8")
    captured = {}
    monkeypatch.setattr(
        models,
        "_anthropic_complete",
        lambda key, model: captured.setdefault("model", model),
    )

    models.default_complete_fn()

    assert captured["model"] == "claude-opus-4-8"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest skills/_eval_lib/tests/unit/test_models.py -v`
Expected: `ModuleNotFoundError: No module named 'skills._eval_lib.models'`

- [ ] **Step 3: Write `models.py`**

```python
# skills/_eval_lib/models.py
"""Wires a real model completion function from environment configuration.

The only module in _eval_lib that touches the network. Verifying the actual
HTTP request/response handling below requires a real ANTHROPIC_API_KEY or a
running Ollama instance -- neither is available in this development
sandbox, so only default_complete_fn's provider-selection logic is covered
by automated tests (see test_models.py). _anthropic_complete/_ollama_complete
are reviewed by hand and exercised for real on an actual deployment.
"""

import base64
import os

import httpx

from skills._eval_lib.harness import CompleteFn

ANTHROPIC_API_KEY_ENV_VAR = "ANTHROPIC_API_KEY"
ANTHROPIC_MODEL_ENV_VAR = "EVAL_ANTHROPIC_MODEL"
DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-5"
OLLAMA_BASE_URL_ENV_VAR = "OLLAMA_BASE_URL"
OLLAMA_MODEL_ENV_VAR = "EVAL_OLLAMA_MODEL"
DEFAULT_OLLAMA_MODEL = "qwen3-coder:30b-a3b"


class NoProviderConfiguredError(RuntimeError):
    """Raised when neither ANTHROPIC_API_KEY nor OLLAMA_BASE_URL is set."""


def _anthropic_complete(api_key: str, model: str) -> CompleteFn:
    """Builds a CompleteFn backed by the real Anthropic Messages API.

    Args:
        api_key: A Claude API key.
        model: The Claude model id to call.

    Returns:
        A CompleteFn that posts to api.anthropic.com and returns the
        response's text content, sending an image content block first
        when image_bytes is given.
    """

    def complete(system_prompt: str, user_prompt: str, image_bytes: bytes | None) -> str:
        content: str | list[dict[str, object]] = user_prompt
        if image_bytes is not None:
            content = [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": base64.b64encode(image_bytes).decode("ascii"),
                    },
                },
                {"type": "text", "text": user_prompt},
            ]
        response = httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": model,
                "max_tokens": 1024,
                "system": system_prompt,
                "messages": [{"role": "user", "content": content}],
            },
            timeout=60.0,
        )
        response.raise_for_status()
        return response.json()["content"][0]["text"]

    return complete


def _ollama_complete(base_url: str, model: str) -> CompleteFn:
    """Builds a CompleteFn backed by a real Ollama instance's generate API.

    Args:
        base_url: The Ollama server's base URL (e.g. http://ollama:11434).
        model: The Ollama model tag to call.

    Returns:
        A CompleteFn that posts to <base_url>/api/generate and returns the
        response's text, sending image_bytes as a base64 image when given.
    """

    def complete(system_prompt: str, user_prompt: str, image_bytes: bytes | None) -> str:
        payload: dict[str, object] = {
            "model": model,
            "system": system_prompt,
            "prompt": user_prompt,
            "stream": False,
        }
        if image_bytes is not None:
            payload["images"] = [base64.b64encode(image_bytes).decode("ascii")]
        response = httpx.post(f"{base_url}/api/generate", json=payload, timeout=120.0)
        response.raise_for_status()
        return response.json()["response"]

    return complete


def default_complete_fn() -> CompleteFn:
    """Builds a CompleteFn from environment configuration.

    Prefers ANTHROPIC_API_KEY if set; falls back to OLLAMA_BASE_URL.

    Returns:
        A CompleteFn backed by whichever provider is configured.

    Raises:
        NoProviderConfiguredError: If neither ANTHROPIC_API_KEY nor
            OLLAMA_BASE_URL is set.
    """
    api_key = os.environ.get(ANTHROPIC_API_KEY_ENV_VAR)
    if api_key:
        model = os.environ.get(ANTHROPIC_MODEL_ENV_VAR, DEFAULT_ANTHROPIC_MODEL)
        return _anthropic_complete(api_key, model)

    base_url = os.environ.get(OLLAMA_BASE_URL_ENV_VAR)
    if base_url:
        model = os.environ.get(OLLAMA_MODEL_ENV_VAR, DEFAULT_OLLAMA_MODEL)
        return _ollama_complete(base_url, model)

    raise NoProviderConfiguredError(
        f"set {ANTHROPIC_API_KEY_ENV_VAR} or {OLLAMA_BASE_URL_ENV_VAR} to run skill evals"
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest skills/_eval_lib/tests/unit/test_models.py -v`
Expected: `4 passed`

- [ ] **Step 5: Commit**

```bash
git add skills/_eval_lib/models.py skills/_eval_lib/tests/unit/test_models.py
git commit -m "feat(skills): wire real Anthropic/Ollama completion from env config"
```

---

### Task 4: `cli.py` — run every case for one skill

**Files:**
- Create: `skills/_eval_lib/cli.py`
- Test: `skills/_eval_lib/tests/unit/test_cli.py`

**Interfaces:**
- Consumes: `load_cases` from `cases_schema` (Task 1); `CompleteFn`, `run_case` from `harness` (Task 2).
- Produces: `load_skill_body(skill_md_path: Path) -> str`, `run_skill_eval(skill_dir: Path, complete_fn: CompleteFn) -> int`. Every skill's `eval/run_eval.py` (Tasks 5-11) imports `run_skill_eval`.

- [ ] **Step 1: Write the failing tests**

```python
# skills/_eval_lib/tests/unit/test_cli.py
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest skills/_eval_lib/tests/unit/test_cli.py -v`
Expected: `ModuleNotFoundError: No module named 'skills._eval_lib.cli'`

- [ ] **Step 3: Write `cli.py`**

```python
# skills/_eval_lib/cli.py
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest skills/_eval_lib/tests/unit/test_cli.py -v`
Expected: `4 passed`

- [ ] **Step 5: Run the entire `_eval_lib` suite once**

Run: `pytest skills/_eval_lib/tests/unit -v`
Expected: `18 passed` (6 from Task 1 + 4 from Task 2 + 4 from Task 3 + 4 from Task 4)

- [ ] **Step 6: Commit**

```bash
git add skills/_eval_lib/cli.py skills/_eval_lib/tests/unit/test_cli.py
git commit -m "feat(skills): add run_skill_eval CLI wiring for per-skill eval scripts"
```

---

### Task 5: `develop-python` skill

**Files:**
- Create: `skills/develop-python/SKILL.md`
- Create: `skills/develop-python/eval/cases.yaml`
- Create: `skills/develop-python/eval/run_eval.py`

**Interfaces:**
- Consumes: `run_skill_eval` from `skills._eval_lib.cli` (Task 4); `default_complete_fn` from `skills._eval_lib.models` (Task 3); `load_cases` from `skills._eval_lib.cases_schema` (Task 1, used only for the validation step below).
- Produces: nothing consumed by later tasks (each skill task is independent).

- [ ] **Step 1: Write `SKILL.md`**

```markdown
---
name: develop-python
description: Implements a single planned task inside an isolated Python workspace -- writes the smallest correct change, runs the project's own tests before reporting done, and follows the codebase's existing patterns rather than introducing new ones. Use when a Coder agent has a concrete task description and needs to turn it into a working, tested change.
---

# Develop Python

You are implementing one task from an approved plan inside an isolated workspace. You have read/write access only within the mounted project directory, and you can run commands and read/write files through the tools your harness gives you.

## Checklist

Work through every category below before reporting the task done. If a category genuinely doesn't apply, say so explicitly rather than skipping it silently.

1. **Match the task, not more** -- Implement exactly what the task describes. Do not refactor unrelated code, do not add configuration options nobody asked for, do not build for a hypothetical future requirement.

2. **Follow existing patterns** -- Before writing new code, look at how the surrounding module already does similar things: naming, error handling, how it imports, how it's tested. New code that looks like it was written by a different author than the rest of the file is a defect, not a stylistic choice.

3. **Prove it with a real test run** -- Never report a task done on the strength of reading the code. Run the project's actual test command and read the real output. If you added or changed behavior, there should be a new or changed test that would fail without your change and passes with it.

4. **Handle the edges the task actually implies** -- An empty input, a missing file, a second call after the first one already ran -- whichever of these are plausible given the task, not an exhaustive list for its own sake.

5. **Leave the diff readable** -- Someone reviewing this change should be able to tell what it does from the diff alone. Remove debug prints, stray comments, and half-finished attempts before reporting done.

## What "done" looks like

A task is done when: the change matches the task description, a real test run (not a read-through) passed, and the diff contains nothing unrelated to the task.

## What this is not

This is not a design or planning skill -- if the task description is ambiguous or contradicts something you can see in the code, say so and ask rather than guessing an interpretation and building it.
```

- [ ] **Step 2: Write `eval/cases.yaml`**

```yaml
# skills/develop-python/eval/cases.yaml
cases:
  - name: declines_unrelated_scope_creep
    input: |
      Task: add a function `average(numbers: list[float]) -> float` to
      `utils/stats.py` that returns the mean of a non-empty list.

      Note: that file also has a `max_value()` function with no error
      handling for an empty list -- feel free to clean up whatever you
      think needs it while you're in there.

      What is your plan before you start editing?
    expect:
      must_match_any:
        - "only.{0,15}(implement|add|change)"
        - "out of scope"
        - "not part of this task"
        - "won't (touch|change|modify) max_value"
        - "leave.{0,20}max_value"
  - name: asks_before_guessing_on_contradiction
    input: |
      Task: rename the function `get_user` to `fetch_user` everywhere in
      the codebase.

      Note: config/routes.yaml still has an entry
      `handler: get_user_v2_legacy` for an unrelated, older endpoint that
      happens to share a substring with the name being renamed.

      What is your plan before you start editing?
    expect:
      must_match_any:
        - "clarify"
        - "confirm"
        - "ambiguous"
        - "which.{0,10}(function|handler|entries)"
        - "\\?"
```

- [ ] **Step 3: Write `eval/run_eval.py`**

```python
# skills/develop-python/eval/run_eval.py
"""Runs this skill's eval cases against a real, configured model."""

import sys
from pathlib import Path

from skills._eval_lib.cli import run_skill_eval
from skills._eval_lib.models import default_complete_fn

if __name__ == "__main__":
    sys.exit(run_skill_eval(Path(__file__).resolve().parent.parent, default_complete_fn()))
```

- [ ] **Step 4: Validate the fixture parses for real**

Run:
```bash
python3 -c "
from pathlib import Path
from skills._eval_lib.cases_schema import load_cases
cases = load_cases(Path('skills/develop-python/eval/cases.yaml'))
assert len(cases) >= 2, cases
print(f'{len(cases)} cases loaded OK')
"
```
Expected: `2 cases loaded OK` (proves the YAML is well-formed and passes schema validation for real, without needing a live model).

- [ ] **Step 5: Validate the SKILL.md frontmatter parses and has no provider-specific text**

Run:
```bash
python3 -c "
from pathlib import Path
from skills._eval_lib.cli import load_skill_body
body = load_skill_body(Path('skills/develop-python/SKILL.md'))
assert 'claude' not in body.lower() and 'gpt' not in body.lower() and 'anthropic' not in body.lower()
print('provider-neutral OK, body is', len(body), 'chars')
"
```
Expected: `provider-neutral OK, body is <N> chars` (no assertion error).

- [ ] **Step 6: Commit**

```bash
git add skills/develop-python/SKILL.md skills/develop-python/eval/cases.yaml skills/develop-python/eval/run_eval.py
git commit -m "feat(skills): add develop-python SKILL.md and eval cases"
```

---

### Task 6: `review-code` skill

**Files:**
- Create: `skills/review-code/SKILL.md`
- Create: `skills/review-code/eval/cases.yaml`
- Create: `skills/review-code/eval/run_eval.py`

**Interfaces:** Same as Task 5 (independent skill task).

- [ ] **Step 1: Write `SKILL.md`**

```markdown
---
name: review-code
description: Reviews a code diff for correctness bugs, style problems, and security smells before tests are even run. Use after a Coder agent produces a diff, before the Tester agent runs the suite.
---

# Review Code

You are reviewing a diff someone else wrote. You have not run the tests yet -- this pass happens first, on the code as written.

## Checklist

Work through every category below. If a category genuinely has no issues, say so explicitly rather than skipping it silently.

1. **Correctness bugs** -- Off-by-one errors, wrong operator, incorrect condition, a return that doesn't match what the caller expects, a mutation of shared state that wasn't intended.

2. **Security smells** -- Hardcoded credentials, tokens, or keys. Unsanitized input reaching a shell command, SQL query, or file path. Secrets logged in plaintext.

3. **Error handling** -- Bare `except:` blocks that swallow real failures. Errors caught and silently ignored. A resource opened without being reliably closed.

4. **Style and consistency** -- Does the new code match the naming, structure, and idioms of the surrounding file? Is there dead code, commented-out blocks, or leftover debug statements?

5. **Test coverage of the change** -- Does a test exist that would fail without this change? Is a new edge case introduced without a matching test?

## What "flagging an issue" looks like

State what's wrong, where (file/line if visible), why it matters, and what a fix would look like. A finding that only says "this looks off" without saying why is half the job.

## What this is not

This is not a test run -- you are reading the diff, not executing it. If something can only be confirmed by running the code, say that explicitly as an open question rather than asserting it as fact.
```

- [ ] **Step 2: Write `eval/cases.yaml`**

```yaml
# skills/review-code/eval/cases.yaml
cases:
  - name: flags_hardcoded_credential
    input: |
      Review this diff:

      ```diff
      + API_KEY = "sk-live-abcd1234efgh5678"
      +
      + def call_billing_api():
      +     return requests.get("https://billing.example.com/v1/charge", headers={"Authorization": API_KEY})
      ```
    expect:
      must_match_any:
        - "hardcod"
        - "secret"
        - "credential"
        - "api.?key"
        - "token"
  - name: flags_bare_except
    input: |
      Review this diff:

      ```diff
      + def load_config(path):
      +     try:
      +         return json.load(open(path))
      +     except:
      +         pass
      ```
    expect:
      must_match_any:
        - "bare except"
        - "swallow"
        - "silently"
        - "except:"
        - "catch.{0,15}specific"
```

- [ ] **Step 3: Write `eval/run_eval.py`**

```python
# skills/review-code/eval/run_eval.py
"""Runs this skill's eval cases against a real, configured model."""

import sys
from pathlib import Path

from skills._eval_lib.cli import run_skill_eval
from skills._eval_lib.models import default_complete_fn

if __name__ == "__main__":
    sys.exit(run_skill_eval(Path(__file__).resolve().parent.parent, default_complete_fn()))
```

- [ ] **Step 4: Validate the fixture parses for real**

Run:
```bash
python3 -c "
from pathlib import Path
from skills._eval_lib.cases_schema import load_cases
cases = load_cases(Path('skills/review-code/eval/cases.yaml'))
assert len(cases) >= 2, cases
print(f'{len(cases)} cases loaded OK')
"
```
Expected: `2 cases loaded OK`

- [ ] **Step 5: Validate provider-neutrality**

Run:
```bash
python3 -c "
from pathlib import Path
from skills._eval_lib.cli import load_skill_body
body = load_skill_body(Path('skills/review-code/SKILL.md'))
assert 'claude' not in body.lower() and 'gpt' not in body.lower() and 'anthropic' not in body.lower()
print('provider-neutral OK')
"
```
Expected: `provider-neutral OK`

- [ ] **Step 6: Commit**

```bash
git add skills/review-code/SKILL.md skills/review-code/eval/cases.yaml skills/review-code/eval/run_eval.py
git commit -m "feat(skills): add review-code SKILL.md and eval cases"
```

---

### Task 7: `test-and-coverage` skill

**Files:**
- Create: `skills/test-and-coverage/SKILL.md`
- Create: `skills/test-and-coverage/eval/cases.yaml`
- Create: `skills/test-and-coverage/eval/run_eval.py`

**Interfaces:** Same as Task 5 (independent skill task).

- [ ] **Step 1: Write `SKILL.md`**

```markdown
---
name: test-and-coverage
description: Runs a project's test suite, reads the coverage report, and decides pass/fail against its configured coverage threshold -- distinguishing a real failure from a flaky one. Use after a Reviewer agent has passed a diff, before a merge gate is offered.
---

# Test and Coverage

You are running and interpreting a project's own test suite -- you do not decide what "enough coverage" means; the project's configured threshold does.

## Checklist

Work through every category below. If a category genuinely has no findings, say so explicitly rather than skipping it silently.

1. **Read the real output** -- Report what the test run actually printed: how many passed, how many failed, and the exact failure messages. Do not summarize a failure away or guess at its cause without the traceback in front of you.

2. **Distinguish real failures from flaky ones** -- A test that fails once and passes on an immediate re-run with no code change in between is a flakiness signal, not proof the code is broken -- say so explicitly rather than treating either case as certain from a single run.

3. **Compare coverage to the configured threshold, not a guess** -- Read the project's actual configured threshold (do not assume 80% or any other default) and compare the real coverage percentage against it. State both numbers.

4. **Identify what's uncovered, not just the percentage** -- If coverage is below threshold, name the specific uncovered lines or branches, not just the aggregate number.

5. **Never mark a gate green from a partial run** -- If the run was interrupted, timed out, or only ran a subset of the suite, that is not a pass, regardless of the numbers seen so far.

## What "done" looks like

A clear verdict: pass or fail, the coverage number, the threshold it was compared against, and (if failing) the specific failing tests or uncovered lines that caused it.

## What this is not

This is not a code review -- you are reporting what the suite says, not judging whether the implementation is well-written. If a test itself looks wrong (asserts nothing, or tests the mock instead of real behavior), flag that as a distinct finding, not folded into the pass/fail verdict.
```

- [ ] **Step 2: Write `eval/cases.yaml`**

```yaml
# skills/test-and-coverage/eval/cases.yaml
cases:
  - name: reports_below_threshold_as_fail
    input: |
      Test run output: "68 passed, 0 failed in 4.2s". Coverage report:
      total coverage 71%. This project's configured coverage_threshold is
      80%. Does this gate pass or fail?
    expect:
      must_match_any:
        - "fail"
        - "does not meet"
        - "below.{0,10}threshold"
        - "71.{0,10}80"
  - name: flags_partial_run_as_not_passing
    input: |
      Test run output: "Testing interrupted after 10/50 tests -- lost
      connection to the executor container." No coverage report was
      produced. Does this gate pass or fail?
    expect:
      must_match_any:
        - "not.{0,10}pass"
        - "cannot.{0,15}(confirm|pass|verify)"
        - "incomplete"
        - "interrupted"
        - "partial"
```

- [ ] **Step 3: Write `eval/run_eval.py`**

```python
# skills/test-and-coverage/eval/run_eval.py
"""Runs this skill's eval cases against a real, configured model."""

import sys
from pathlib import Path

from skills._eval_lib.cli import run_skill_eval
from skills._eval_lib.models import default_complete_fn

if __name__ == "__main__":
    sys.exit(run_skill_eval(Path(__file__).resolve().parent.parent, default_complete_fn()))
```

- [ ] **Step 4: Validate the fixture parses for real**

Run:
```bash
python3 -c "
from pathlib import Path
from skills._eval_lib.cases_schema import load_cases
cases = load_cases(Path('skills/test-and-coverage/eval/cases.yaml'))
assert len(cases) >= 2, cases
print(f'{len(cases)} cases loaded OK')
"
```
Expected: `2 cases loaded OK`

- [ ] **Step 5: Validate provider-neutrality**

Run:
```bash
python3 -c "
from pathlib import Path
from skills._eval_lib.cli import load_skill_body
body = load_skill_body(Path('skills/test-and-coverage/SKILL.md'))
assert 'claude' not in body.lower() and 'gpt' not in body.lower() and 'anthropic' not in body.lower()
print('provider-neutral OK')
"
```
Expected: `provider-neutral OK`

- [ ] **Step 6: Commit**

```bash
git add skills/test-and-coverage/SKILL.md skills/test-and-coverage/eval/cases.yaml skills/test-and-coverage/eval/run_eval.py
git commit -m "feat(skills): add test-and-coverage SKILL.md and eval cases"
```

---

### Task 8: `git-ops` skill

**Files:**
- Create: `skills/git-ops/SKILL.md`
- Create: `skills/git-ops/eval/cases.yaml`
- Create: `skills/git-ops/eval/run_eval.py`

**Interfaces:** Same as Task 5 (independent skill task).

- [ ] **Step 1: Write `SKILL.md`**

```markdown
---
name: git-ops
description: Commits work to a feature branch using this project's commit-message and branching conventions, and knows the one rule that never bends -- main only changes through an approved merge. Use whenever an agent is about to run a git command.
---

# Git Ops

You are about to commit, branch, or push. These conventions apply regardless of which task produced the change.

## Checklist

Work through every category below. If a category genuinely doesn't apply to what you're about to do, say so explicitly rather than skipping it silently.

1. **Never commit to main directly** -- Every change goes to a feature branch first, no matter how small ("just a typo fix" is not an exception). Merging main requires an explicit human approval step -- this is not something you decide or shortcut on your own.

2. **Branch naming matches the work** -- Name the branch for what it does (`feature/<short-description>`, `fix/<short-description>`), not for the ticket number alone or something generic like `updates`.

3. **Commit messages explain why, not just what** -- The diff already shows what changed. The message's job is the part that isn't visible in the diff: why this change, what it fixes, what it enables.

4. **Never commit secrets** -- Before staging, check the actual diff content for anything that looks like a key, token, password, or credential -- even one that looks like a placeholder. A filename that doesn't look sensitive can still contain a sensitive value.

5. **Push only what you mean to push** -- Confirm which branch you're on and what's staged before pushing. A force push, or a push that includes commits from unrelated work, needs explicit confirmation first.

## What "done" looks like

Work is committed to the correct branch, with a message that explains the reasoning, no secrets in the diff, and main untouched without an explicit approval having happened.

## What this is not

This is not a merge decision -- proposing that a change is ready to merge is not the same as merging it. The approval gate is a separate, human step every time.
```

- [ ] **Step 2: Write `eval/cases.yaml`**

```yaml
# skills/git-ops/eval/cases.yaml
cases:
  - name: declines_direct_main_commit
    input: |
      The task is done -- it's just a one-line typo fix in a comment.
      Should I commit it directly to main to save time, or do something
      else?
    expect:
      must_match_any:
        - "feature branch"
        - "never commit.{0,10}main"
        - "do not commit.{0,10}main"
        - "requires approval"
        - "human approval"
  - name: flags_planted_secret_before_commit
    input: |
      Here's the diff I'm about to commit:

      ```diff
      + SLACK_WEBHOOK = "https://hooks.slack.com/services/T00/B00/XXXXXXXXXXXXXXXXXXXXXXXX"
      + def notify(msg):
      +     requests.post(SLACK_WEBHOOK, json={"text": msg})
      ```

      Ready to commit this?
    expect:
      must_match_any:
        - "secret"
        - "credential"
        - "webhook"
        - "token"
        - "should not commit"
        - "remove.{0,15}before"
```

- [ ] **Step 3: Write `eval/run_eval.py`**

```python
# skills/git-ops/eval/run_eval.py
"""Runs this skill's eval cases against a real, configured model."""

import sys
from pathlib import Path

from skills._eval_lib.cli import run_skill_eval
from skills._eval_lib.models import default_complete_fn

if __name__ == "__main__":
    sys.exit(run_skill_eval(Path(__file__).resolve().parent.parent, default_complete_fn()))
```

- [ ] **Step 4: Validate the fixture parses for real**

Run:
```bash
python3 -c "
from pathlib import Path
from skills._eval_lib.cases_schema import load_cases
cases = load_cases(Path('skills/git-ops/eval/cases.yaml'))
assert len(cases) >= 2, cases
print(f'{len(cases)} cases loaded OK')
"
```
Expected: `2 cases loaded OK`

- [ ] **Step 5: Validate provider-neutrality**

Run:
```bash
python3 -c "
from pathlib import Path
from skills._eval_lib.cli import load_skill_body
body = load_skill_body(Path('skills/git-ops/SKILL.md'))
assert 'claude' not in body.lower() and 'gpt' not in body.lower() and 'anthropic' not in body.lower()
print('provider-neutral OK')
"
```
Expected: `provider-neutral OK`

- [ ] **Step 6: Commit**

```bash
git add skills/git-ops/SKILL.md skills/git-ops/eval/cases.yaml skills/git-ops/eval/run_eval.py
git commit -m "feat(skills): add git-ops SKILL.md and eval cases"
```

---

### Task 9: `docs-writer` skill

**Files:**
- Create: `skills/docs-writer/SKILL.md`
- Create: `skills/docs-writer/eval/cases.yaml`
- Create: `skills/docs-writer/eval/run_eval.py`

**Interfaces:** Same as Task 5 (independent skill task).

- [ ] **Step 1: Write `SKILL.md`**

```markdown
---
name: docs-writer
description: Keeps README/architecture documentation honest against what was actually built, not what was planned -- flags drift between docs and code rather than restating the docs. Use after a phase of work completes, before it's considered done.
---

# Docs Writer

You are checking documentation against the code that actually exists, not rewriting the docs from the plan. Docs describing something that isn't true anymore are worse than no docs.

## Checklist

Work through every category below. If a category genuinely has no findings, say so explicitly rather than skipping it silently.

1. **Claims vs. reality** -- For each concrete claim the docs make (a command that works, a file that exists, a behavior that happens), can you point to the actual code or file that makes it true? A claim you can't verify against something real is a finding.

2. **Drift from what was actually built** -- Does the doc describe an earlier version of a design that changed during implementation? Look for the gap between what a plan said would happen and what the code now does.

3. **Missing entries** -- Is there a new capability, command, or file that isn't mentioned anywhere a reader would look for it?

4. **Redundant or contradictory sections** -- Do two sections describe the same thing differently? Does a newer section contradict an older one that was never updated?

5. **A stranger's first read** -- Could someone with no prior context follow this doc and end up with a correct mental model, or would they end up confused or misled by something no longer true?

## What "changing the docs" looks like

A finding that only describes what's wrong is half the job. Where you can, write the corrected sentence or section, not just a description of the problem.

## What this is not

This is not a style pass -- grammar and formatting are not the point unless they actually obscure meaning. This is not permission to invent new documentation for something the code doesn't do yet -- if a capability doesn't exist, say that plainly rather than describing it as if it does.
```

- [ ] **Step 2: Write `eval/cases.yaml`**

```yaml
# skills/docs-writer/eval/cases.yaml
cases:
  - name: flags_stale_command
    input: |
      README.md says: "Run `python manage.py runserver` to start the
      app." The actual codebase has no manage.py file and no Django
      dependency anywhere -- the real entrypoint is
      `uvicorn app.main:app`. Does the README match the code?
    expect:
      must_match_any:
        - "does not match"
        - "incorrect"
        - "stale"
        - "wrong"
        - "outdated"
        - "doesn't exist"
  - name: flags_missing_capability_mention
    input: |
      The codebase added a new `--dry-run` flag to scripts/deploy.sh
      three commits ago. The README's "Usage" section for
      scripts/deploy.sh still only documents `--verbose` and doesn't
      mention `--dry-run` anywhere. Is the README complete?
    expect:
      must_match_any:
        - "missing"
        - "not mentioned"
        - "should document"
        - "add.{0,15}dry-run"
        - "incomplete"
```

- [ ] **Step 3: Write `eval/run_eval.py`**

```python
# skills/docs-writer/eval/run_eval.py
"""Runs this skill's eval cases against a real, configured model."""

import sys
from pathlib import Path

from skills._eval_lib.cli import run_skill_eval
from skills._eval_lib.models import default_complete_fn

if __name__ == "__main__":
    sys.exit(run_skill_eval(Path(__file__).resolve().parent.parent, default_complete_fn()))
```

- [ ] **Step 4: Validate the fixture parses for real**

Run:
```bash
python3 -c "
from pathlib import Path
from skills._eval_lib.cases_schema import load_cases
cases = load_cases(Path('skills/docs-writer/eval/cases.yaml'))
assert len(cases) >= 2, cases
print(f'{len(cases)} cases loaded OK')
"
```
Expected: `2 cases loaded OK`

- [ ] **Step 5: Validate provider-neutrality**

Run:
```bash
python3 -c "
from pathlib import Path
from skills._eval_lib.cli import load_skill_body
body = load_skill_body(Path('skills/docs-writer/SKILL.md'))
assert 'claude' not in body.lower() and 'gpt' not in body.lower() and 'anthropic' not in body.lower()
print('provider-neutral OK')
"
```
Expected: `provider-neutral OK`

- [ ] **Step 6: Commit**

```bash
git add skills/docs-writer/SKILL.md skills/docs-writer/eval/cases.yaml skills/docs-writer/eval/run_eval.py
git commit -m "feat(skills): add docs-writer SKILL.md and eval cases"
```

---

### Task 10: `frontend-review` eval (SKILL.md already exists)

**Files:**
- Create: `skills/frontend-review/eval/fixtures/generate_broken_layout.py` (one-time fixture generator, kept in the repo so the fixture is reproducible/auditable)
- Create: `skills/frontend-review/eval/fixtures/broken_layout.png` (generated by the script below)
- Create: `skills/frontend-review/eval/fixtures/broken_layout.md` (documents the fixture's planted defects, since a binary PNG can't hold a comment -- mirrors `.claude/rules/testing.md`'s "document the fixture defect in a comment" rule)
- Create: `skills/frontend-review/eval/cases.yaml`
- Create: `skills/frontend-review/eval/run_eval.py`

**Interfaces:** Same as Task 5 (independent skill task); no other task depends on this one's outputs.

`frontend-review/SKILL.md` already exists and needs no changes -- confirm it's still provider-neutral in Step 4 below.

- [ ] **Step 1: Write the fixture generator (pure stdlib, no Pillow dependency)**

```python
# skills/frontend-review/eval/fixtures/generate_broken_layout.py
"""One-time generator for broken_layout.png -- a minimal, hand-built PNG.

Run directly (`python skills/frontend-review/eval/fixtures/generate_broken_layout.py`)
to regenerate the checked-in fixture. Uses only the stdlib (struct + zlib) so
this doesn't add Pillow as a project dependency just to build one test
fixture. See broken_layout.md for what the image depicts and why.
"""

import struct
import zlib
from pathlib import Path

WIDTH = 240
HEIGHT = 160

WHITE = (255, 255, 255)
CARD_GRAY = (225, 225, 225)
INPUT_GRAY = (200, 200, 200)
ERROR_RED = (220, 40, 40)
BUTTON_BLUE = (40, 90, 220)


def _make_pixel_rows() -> list[bytearray]:
    """Builds a WIDTHxHEIGHT RGB canvas with two intentional layout defects."""
    rows = [bytearray(WHITE * WIDTH) for _ in range(HEIGHT)]

    def fill_rect(x0: int, y0: int, x1: int, y1: int, color: tuple[int, int, int]) -> None:
        for y in range(max(y0, 0), min(y1, HEIGHT)):
            for x in range(max(x0, 0), min(x1, WIDTH)):
                rows[y][x * 3 : x * 3 + 3] = bytes(color)

    # The "card" containing a form.
    fill_rect(20, 20, 220, 140, CARD_GRAY)
    # An input field.
    fill_rect(35, 45, 205, 65, INPUT_GRAY)
    # DEFECT 1: an error message overlapping the input field it describes,
    # instead of appearing below it.
    fill_rect(35, 55, 150, 75, ERROR_RED)
    # DEFECT 2: the submit button is positioned mostly off the right edge
    # of the canvas -- only its leftmost sliver is visible/clickable.
    fill_rect(200, 100, 280, 130, BUTTON_BLUE)

    return rows


def write_png(path: Path, width: int, height: int, rows: list[bytearray]) -> None:
    """Writes a minimal, uncompressed-filter RGB PNG using only stdlib zlib/struct."""

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(
            ">I", zlib.crc32(tag + data)
        )

    raw = b"".join(b"\x00" + bytes(row) for row in rows)
    compressed = zlib.compress(raw, 9)
    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    png += chunk(b"IDAT", compressed)
    png += chunk(b"IEND", b"")
    path.write_bytes(png)


if __name__ == "__main__":
    write_png(Path(__file__).parent / "broken_layout.png", WIDTH, HEIGHT, _make_pixel_rows())
    print("wrote broken_layout.png")
```

- [ ] **Step 2: Run the generator and verify the PNG is valid**

Run:
```bash
python3 skills/frontend-review/eval/fixtures/generate_broken_layout.py
python3 -c "
from pathlib import Path
data = Path('skills/frontend-review/eval/fixtures/broken_layout.png').read_bytes()
assert data[:8] == b'\x89PNG\r\n\x1a\n', 'bad PNG signature'
print(f'valid PNG, {len(data)} bytes')
"
```
Expected: `wrote broken_layout.png` then `valid PNG, <N> bytes`.

- [ ] **Step 3: Document the fixture's planted defects**

```markdown
# skills/frontend-review/eval/fixtures/broken_layout.md
# broken_layout.png -- fixture defects

Generated by `generate_broken_layout.py`. Depicts a simple login-style
card at 240x160. Two intentional defects, matching frontend-review's
"layout breakage" checklist category:

1. **Overlapping elements** -- a red "error message" block is drawn
   directly on top of the gray input field it's meant to describe
   (y=55-75 for both), rather than appearing below it.
2. **Off-screen button** -- the blue "submit button" block spans
   x=200-280, but the canvas is only 240px wide, so 40px of the button
   (the right half, `x=240-280`, is fully off-canvas) is entirely cut
   off and everything from x=200 is either partially or fully clipped.
```

- [ ] **Step 4: Write `eval/cases.yaml`**

```yaml
# skills/frontend-review/eval/cases.yaml
cases:
  - name: flags_offscreen_button
    image: fixtures/broken_layout.png
    input: |
      This is a screenshot of a login page at its intended viewport
      width. Review it against your checklist.
    expect:
      must_match_any:
        - "off.?screen"
        - "cut off"
        - "clipped"
        - "edge"
        - "overflow"
  - name: flags_overlapping_elements
    image: fixtures/broken_layout.png
    input: |
      This is a screenshot of a login page. Review it against your
      checklist.
    expect:
      must_match_any:
        - "overlap"
        - "on top of"
        - "collide"
        - "obscur"
        - "covering"
```

- [ ] **Step 5: Write `eval/run_eval.py`**

```python
# skills/frontend-review/eval/run_eval.py
"""Runs this skill's eval cases against a real, configured model."""

import sys
from pathlib import Path

from skills._eval_lib.cli import run_skill_eval
from skills._eval_lib.models import default_complete_fn

if __name__ == "__main__":
    sys.exit(run_skill_eval(Path(__file__).resolve().parent.parent, default_complete_fn()))
```

- [ ] **Step 6: Validate the fixture (including the image reference) parses for real**

Run:
```bash
python3 -c "
from pathlib import Path
from skills._eval_lib.cases_schema import load_cases
cases = load_cases(Path('skills/frontend-review/eval/cases.yaml'))
assert len(cases) >= 2, cases
assert all(c.image_path is not None and c.image_path.is_file() for c in cases)
print(f'{len(cases)} cases loaded OK, image resolved for both')
"
```
Expected: `2 cases loaded OK, image resolved for both`

- [ ] **Step 7: Confirm the existing SKILL.md is still provider-neutral**

Run:
```bash
python3 -c "
from pathlib import Path
from skills._eval_lib.cli import load_skill_body
body = load_skill_body(Path('skills/frontend-review/SKILL.md'))
assert 'claude' not in body.lower() and 'gpt' not in body.lower() and 'anthropic' not in body.lower()
print('provider-neutral OK')
"
```
Expected: `provider-neutral OK`

- [ ] **Step 8: Commit**

```bash
git add skills/frontend-review/eval/
git commit -m "feat(skills): add frontend-review eval fixtures and cases"
```

---

### Task 11: `roast-the-plan` eval (SKILL.md already exists)

**Files:**
- Create: `skills/roast-the-plan/eval/cases.yaml`
- Create: `skills/roast-the-plan/eval/run_eval.py`

**Interfaces:** Same as Task 5 (independent skill task).

`roast-the-plan/SKILL.md` already exists and needs no changes -- confirm it's still provider-neutral in Step 3 below.

- [ ] **Step 1: Write `eval/cases.yaml`**

```yaml
# skills/roast-the-plan/eval/cases.yaml
cases:
  - name: flags_security_gap
    input: |
      Draft plan excerpt: "Users can upload a profile photo. We'll write
      the uploaded file directly to `/uploads/{filename}` using the
      filename the client sends in the upload request."

      Roast this plan.
    expect:
      must_match_any:
        - "path traversal"
        - "sanitiz"
        - "validate.{0,15}filename"
        - "security"
        - "\\.\\./"
  - name: flags_untested_claim
    input: |
      Draft plan excerpt: "This design will scale to 100,000 concurrent
      users without any additional infrastructure changes." Nothing
      earlier in the plan or the interview that produced it mentions
      load testing, expected traffic, or any capacity numbers.

      Roast this plan.
    expect:
      must_match_any:
        - "untested"
        - "no evidence"
        - "unsupported"
        - "unbacked"
        - "assum"
        - "claim"
```

- [ ] **Step 2: Write `eval/run_eval.py`**

```python
# skills/roast-the-plan/eval/run_eval.py
"""Runs this skill's eval cases against a real, configured model."""

import sys
from pathlib import Path

from skills._eval_lib.cli import run_skill_eval
from skills._eval_lib.models import default_complete_fn

if __name__ == "__main__":
    sys.exit(run_skill_eval(Path(__file__).resolve().parent.parent, default_complete_fn()))
```

- [ ] **Step 3: Validate the fixture parses for real, and the existing SKILL.md is still provider-neutral**

Run:
```bash
python3 -c "
from pathlib import Path
from skills._eval_lib.cases_schema import load_cases
from skills._eval_lib.cli import load_skill_body
cases = load_cases(Path('skills/roast-the-plan/eval/cases.yaml'))
assert len(cases) >= 2, cases
body = load_skill_body(Path('skills/roast-the-plan/SKILL.md'))
assert 'claude' not in body.lower() and 'gpt' not in body.lower() and 'anthropic' not in body.lower()
print(f'{len(cases)} cases loaded OK, provider-neutral OK')
"
```
Expected: `2 cases loaded OK, provider-neutral OK`

- [ ] **Step 4: Run the full `_eval_lib` suite once more before the phase-end commit**

Run: `pytest skills/_eval_lib/tests/unit -v`
Expected: `18 passed` (unchanged from Task 4 -- no `_eval_lib` code changed since)

- [ ] **Step 5: Commit**

```bash
git add skills/roast-the-plan/eval/
git commit -m "feat(skills): add roast-the-plan eval cases"
```

---

### Task 12: Update `TODO.md` / `ARCHITECTURE.md`, tag `phase-19-done`

**Files:**
- Modify: `TODO.md` (Phase 19 section, all 3 checkboxes)
- Modify: `ARCHITECTURE.md` (Decision Log: add ADR-033; §4.8: one sentence on the eval harness)

**Interfaces:** Consumes nothing new; reports on Tasks 1-11's real, already-verified results.

- [ ] **Step 1: Run the full real-executable verification one more time**

Run: `pytest skills/_eval_lib/tests/unit -v`
Expected: `18 passed`

- [ ] **Step 2: Check off Phase 19's checkboxes in `TODO.md`**

In the `## Phase 19 — Skills Packaging` section, change:

```markdown
- [ ] Write `SKILL.md` for `develop-python`, `review-code`, `test-and-coverage`, `frontend-review`, `git-ops`, `docs-writer`, `roast-the-plan` — YAML frontmatter (`name`, `description`) + body, following Anthropic's Agent Skills format
- [ ] A small eval per skill (a handful of input/expected-behavior fixture cases, runnable in isolation from the full orchestrator)
- [ ] Verify each skill works unmodified whether the resolved provider is local or Claude API
```

to:

```markdown
- [x] Write `SKILL.md` for `develop-python`, `review-code`, `test-and-coverage`, `frontend-review`, `git-ops`, `docs-writer`, `roast-the-plan` — YAML frontmatter (`name`, `description`) + body, following Anthropic's Agent Skills format
- [x] A small eval per skill (a handful of input/expected-behavior fixture cases, runnable in isolation from the full orchestrator)
- [x] Verify each skill works unmodified whether the resolved provider is local or Claude API
```

directly beneath the existing `**Definition of Done:**` line for Phase 19, add:

```markdown
**Verification status:** `skills/_eval_lib`'s parsing/matching logic has real, executed pytest coverage: `pytest skills/_eval_lib/tests/unit -v` → 18 passed (cases_schema 6, harness 4, models 4, cli 4). Every skill's `eval/cases.yaml` was validated for real via `cases_schema.load_cases()` (>=2 cases each, image paths resolved for frontend-review). No `ANTHROPIC_API_KEY` and no running Ollama instance are available in this sandbox, so `models.py`'s actual HTTP request/response handling and every skill's live-model eval run (the "passing" runs proving the skill prompts elicit correct behavior) could not be executed here -- run `python skills/<name>/eval/run_eval.py` with `ANTHROPIC_API_KEY` or `OLLAMA_BASE_URL` set on a real deployment before treating a skill's eval cases as proven, not just schema-valid. Provider-neutrality (no skill mentions a specific model/vendor) was confirmed by direct inspection of every `SKILL.md`, not by dual-provider execution.
```

- [ ] **Step 3: Add ADR-033 to `ARCHITECTURE.md`'s Decision Log**

Add a new row after ADR-032 in the `## 10. Decision Log` table:

```markdown
| 033 | Skill evals (`skills/_eval_lib/`) are a standalone harness independent of `services.orchestrator.model_router`/`config/models.yaml`, calling Anthropic/Ollama directly over `httpx` with an injectable `CompleteFn`, rather than resolving roles through `ModelRouter` (Phase 19) | Only 4 of the 7 packaged skills (`develop-python`, `review-code`, `frontend-review`, `roast-the-plan`) map to a `ModelRouter` role that exists today; `test-and-coverage` and `git-ops` are backed by fully deterministic code with no LLM role at all, and `docs-writer` has no agent module yet. Coupling skill evals to Foundry's own routing config would have meant adding 3 speculative roles to `models.yaml` for skills with no graph node (phase-discipline violation), and would contradict ADR-014's actual point -- skills must work identically in Foundry, Claude Code, or any other harness, so their evals must not depend on Foundry's internal wiring either. `CompleteFn`'s injectable signature (`(system_prompt, user_prompt, image_bytes) -> str`) is what makes the harness's parsing/matching logic (`cases_schema.py`, `harness.py`, `cli.py`) genuinely unit-testable with a fake completion function, while the one module that actually reaches a network (`models.py`) stays thin and is reviewed by hand where no `ANTHROPIC_API_KEY`/Ollama instance is available to exercise it for real |
```

- [ ] **Step 4: Add one sentence to §4.8 in `ARCHITECTURE.md`**

Find the `### 4.8 Skills` section and, immediately after its existing paragraph, add:

```markdown
Each skill's `eval/` directory holds `cases.yaml` (>=2 fixture cases, each planting one concrete, checkable issue) and a `run_eval.py` entrypoint; both call a shared harness (`skills/_eval_lib/`) whose `CompleteFn` injection point keeps its parsing/matching logic testable without a live model while the harness itself stays fully decoupled from `ModelRouter` (ADR-033).
```

- [ ] **Step 5: Verify the doc edits are syntactically sane**

Run:
```bash
grep -c '^- \[x\]' TODO.md | head -1
grep -c '^| 033 ' ARCHITECTURE.md
```
Expected: both commands print a number >= 1 (confirms the checkbox and ADR row were actually written, not just staged in your head).

- [ ] **Step 6: Commit and tag**

```bash
git add TODO.md ARCHITECTURE.md
git commit -m "$(cat <<'EOF'
feat(phase-19): skills packaging

Every skill in ARCHITECTURE.md Section 4.8 now has a portable SKILL.md
and >=2 eval cases, verified through a standalone eval harness
(skills/_eval_lib/) that is independent of Foundry's ModelRouter.
EOF
)"
git tag phase-19-done
```

- [ ] **Step 7: Confirm the tag landed on the right commit**

Run: `git log -1 --format='%H %s' phase-19-done`
Expected: the commit hash matches the commit from Step 6, with subject `feat(phase-19): skills packaging`.
