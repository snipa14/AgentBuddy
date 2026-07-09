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
