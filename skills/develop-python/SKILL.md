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
