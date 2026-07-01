---
description: Testing conventions, coverage requirements, and quality gate rules for Foundry.
---

# Testing Rules

## Coverage floor

The default coverage threshold for the Foundry platform itself is **80%**. Do not commit a phase without hitting it. Check with:

```bash
pytest --cov=services --cov-fail-under=80 --cov-report=term-missing
```

The `--cov-fail-under=80` flag is the enforcement mechanism — do not remove it from CI or the pre-commit hook. If a specific module is intentionally excluded from coverage (e.g., a thin CLI entrypoint), add it to `[tool.coverage.omit]` in `pyproject.toml` with a comment stating why.

## Test file layout

```
services/<name>/
  tests/
    unit/           pure Python, no real Docker, no network, no filesystem (use tmp_path)
    integration/    real Docker, real SQLite, real filesystem — marked @pytest.mark.integration
```

Run unit tests alone: `pytest services/<name>/tests/unit`
Run integration tests (needs Docker daemon): `pytest -m integration services/<name>/tests/integration`

CI always runs both. Locally, unit tests are fast enough to run on every save.

## What to test, specifically

Agent nodes are LLM calls, which makes them non-deterministic. Test the **structure** of the output, not the content:
- Does `node_planner()` return a `ProjectState` with `phase == "awaiting_plan_approval"` and a non-empty `plan` dict? Yes → pass.
- Does the plan dict contain the right keys per the Pydantic schema? Yes → pass.
- Does a specific word appear in the critique? This is a flaky test. Do not write it.

For LLM-backed agent tests: mock the provider at the `model_router.resolve()` boundary, not deeper. This tests that the agent correctly interprets the LLM output, not that the LLM produces a specific string.

For Reviewer and Tester agents that must find real bugs:
- Use a fixture with a deliberately planted defect (off-by-one, hardcoded credential, missing null check).
- Run the test **5 times** and assert the defect is flagged in at least 4 of them. One pass is luck; 4/5 is a reliability signal.
- Document the fixture defect in a comment on the fixture file (this is the one place inline comments are permitted — on test fixtures, to document the intentional flaw).

## interrupt() tests

Every `interrupt()` point in the graph must have a test that:
1. Starts a graph run and verifies the interrupt fires.
2. Verifies the checkpointer has persisted state (simulate a process restart by creating a new graph instance from the same SQLite file).
3. Resumes with `Command(resume=...)` and verifies the graph continues to the correct next node.

A graph that can't survive a restart is broken. Catch this in tests, not in production at 2am.

## Test naming

`test_<thing_under_test>_<condition>_<expected_outcome>`

Examples:
- `test_resolve_local_preferred_when_ollama_available`
- `test_write_file_raises_on_path_traversal`
- `test_planner_node_returns_plan_approval_phase`
- `test_cost_interrupt_fires_at_threshold`

## Do not test

- `SKILL.md` content via the full orchestrator stack — use the per-skill eval scripts in `skills/<name>/eval/`.
- Third-party library behavior (e.g., that `aiogram` parses a message correctly).
- Formatting or string output from LLMs — these are content tests, not behavioral tests.
