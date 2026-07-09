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
