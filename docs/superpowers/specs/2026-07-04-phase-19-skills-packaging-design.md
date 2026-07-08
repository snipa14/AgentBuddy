# Phase 19 — Skills Packaging: Design

**Status:** Approved
**Phase:** TODO.md Phase 19 ("Skills Packaging")
**Decisions:** ADR-014 (skills authored as portable `SKILL.md`)

## Goal

Every skill referenced in `ARCHITECTURE.md` §4.8 exists as a portable, independently-testable `SKILL.md`:

```
skills/
  develop-python/SKILL.md
  review-code/SKILL.md
  test-and-coverage/SKILL.md
  frontend-review/SKILL.md   (exists)
  git-ops/SKILL.md
  docs-writer/SKILL.md
  roast-the-plan/SKILL.md    (exists)
```

Definition of Done (TODO.md): every skill has at least 2 passing eval cases; no skill file contains provider-specific instructions.

## Current State

`frontend-review/SKILL.md` and `roast-the-plan/SKILL.md` already exist and establish the house style: YAML frontmatter (`name`, `description`), a numbered checklist section, a "what changing X looks like" section, and a "what this is not" boundary section. Neither has an `eval/` directory yet — the eval mechanism itself has never been built, for any skill.

Of the 5 remaining skills, only `develop-python` (→ Coder) and `review-code` (→ Reviewer) map to an LLM-backed role already declared in `config/models.yaml` (`coder`, `reviewer`). `test-and-coverage` and `git-ops` are backed today by fully deterministic code (`tester.py`'s `evaluate_gate`, `node_done`'s git commands via `IntegratorClient`) — no LLM call, no corresponding role. `docs-writer` has no agent module at all yet (listed in `ARCHITECTURE.md` §4.7's role table, but never wired into the graph).

## Decision: Eval Mechanism

Evals do **not** depend on `services.orchestrator.model_router` / `config/models.yaml`. A standalone shared harness lives alongside the skills and calls a real model directly, configured from plain env vars (`ANTHROPIC_API_KEY` / `OLLAMA_BASE_URL`), independent of Foundry's internal routing config.

**Why:** Only 4 of 7 skills have a production role to reuse today; forcing a 1:1 mapping would mean adding 3 speculative roles to `models.yaml` for skills with no agent node yet — expanding scope beyond this phase (CLAUDE.md's phase-discipline rule). More importantly, ADR-014's whole point is that skills work identically in Foundry, Claude Code, or any other harness — coupling their evals to Foundry's own `ModelRouter` would contradict that. A skill's eval should be able to run against "some capable model," full stop.

## Harness Architecture

```
skills/_eval_lib/
  harness.py       # pure logic: run_case(complete_fn, skill_body, case) -> EvalResult
  models.py        # env-var-driven Anthropic/Ollama completion fn (the only network-touching code)
  cases_schema.py  # loads + validates cases.yaml into typed EvalCase objects
  tests/unit/      # tests harness.py + cases_schema.py with a fake complete_fn — real, runnable, no network

skills/<name>/
  SKILL.md
  eval/
    cases.yaml     # >=2 fixture cases
    run_eval.py    # thin per-skill entrypoint: loads SKILL.md + cases.yaml, calls harness.run_case per case
```

`complete_fn` has signature `Callable[[str, str], str]` — `(system_prompt, user_prompt) -> completion_text`. Injecting it is what makes `harness.py` genuinely unit-testable without any API key or Ollama instance: tests pass a fake `complete_fn` that returns a scripted string, and assert `run_case` correctly parses `cases.yaml` entries and evaluates `must_match_any` against the (fake) completion.

`models.py` is the only piece that imports `langchain_anthropic`/`langchain_ollama`-equivalent or raw `anthropic`/`httpx` calls to actually reach a model. It is deliberately thin and excluded from automated testing here the same way a CLI entrypoint is (`pyproject.toml`'s `[tool.coverage.omit]` already has precedent for this class of file) — it can only be exercised for real on a deployment with real credentials/Ollama.

Each skill's `eval/run_eval.py` is boilerplate: read `SKILL.md`'s body (everything after the frontmatter), read `cases.yaml`, call `harness.run_case` per case using `models.default_complete_fn()`, print PASS/FAIL per case, exit non-zero on any failure. This satisfies `.claude/rules/testing.md`'s "per-skill eval scripts in `skills/<name>/eval/`" literally, without duplicating logic seven times.

## Fixture Case Format

`skills/<name>/eval/cases.yaml`:

```yaml
cases:
  - name: flags_hardcoded_credential
    input: |
      <diff or scenario text with a planted defect>
    expect:
      must_match_any: ["hardcod", "secret", "credential", "api key"]
  - name: flags_missing_error_handling
    input: |
      <diff or scenario text with a different planted defect>
    expect:
      must_match_any: ["bare except", "swallow", "error handling"]
```

`must_match_any` is a case-insensitive substring/regex list — matching on *category*, not exact wording, per `.claude/rules/testing.md`'s explicit warning against asserting a specific word appears in LLM output. Each case plants one concrete, checkable issue and asserts the skill's guidance causes the model to flag that category of problem. Every skill gets at least 2 cases (Definition of Done).

For `test-and-coverage`, `git-ops`, and `docs-writer` — skills with no production agent role today — the eval still runs the same way (skill body + fixture scenario fed to a generically-capable model via `models.default_complete_fn()`); the eval proves the *skill prompt* elicits correct behavior, independent of whether Foundry has wired an agent node to it yet.

## Verification of "provider-agnostic" (TODO.md checklist item 3)

This is a static property of the skill file content, verified by inspection/grep (no hardcoded model names, no "as Claude..." phrasing, no provider-specific formatting assumptions) rather than by literally running both a local and a Claude-API model in this sandbox. The harness's `complete_fn` injection point being provider-agnostic by construction (same interface for either provider) is what makes this true mechanically, not something proven by dual execution.

## What Can Actually Run in This Sandbox

No `ANTHROPIC_API_KEY` and no Ollama instance are available in this development sandbox — the same class of environmental gap as `langgraph` being absent for Phase 18. Concretely:

- `harness.py` and `cases_schema.py`: real pytest coverage using a fake `complete_fn`, executable and verified in this sandbox.
- `models.py` and each skill's `run_eval.py`: cannot be executed for real here. Reviewed for correctness by hand-trace; real "passing" runs happen on an actual deployment with real credentials/Ollama running. This gets documented plainly in the phase-end TODO.md verification note, not silently claimed as run.

## Out of Scope

- Adding `test-and-coverage`/`git-ops`/`docs-writer` as real `ModelRouter` roles or wiring a `node_docs_writer` graph node — that's new agent-node work, not skill packaging, and isn't required by this phase's checklist.
- A "run N times, require M/N" reliability mode (used for the Reviewer/Tester agent-node tests per `.claude/rules/testing.md`) — not required by this phase's Definition of Done (2 passing cases, not a reliability bar); can be added later per-skill if a specific eval proves flaky.
