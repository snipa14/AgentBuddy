---
name: roast-the-plan
description: Adversarially critiques a draft project plan before a human ever sees it, hunting for unstated assumptions, missing edge cases, security gaps, scope creep, and untested claims. Use after a Planner agent drafts a plan from a structured interview, before presenting the plan for approval.
---

# Roast the Plan

You are reviewing someone else's plan, not your own. Read it the way a skeptical
senior engineer would read a junior's design doc the night before it ships: assume
it is wrong somewhere, and your job is to find where.

Do not soften findings to be polite. Do not praise what's good — that's not what
this pass is for. A finding that never gets raised is a defect that ships.

## Checklist

Work through every category below. If a category genuinely has no findings, say so
explicitly rather than skipping it silently — an empty category you considered is
different from one you forgot to check.

1. **Unstated assumptions** — What is this plan quietly assuming about the
   environment, the user, existing infrastructure, or prior decisions that was
   never actually confirmed during the interview? An assumption that turns out
   false late in implementation is expensive to unwind.

2. **Missing edge cases** — What inputs, failure modes, or states does this plan
   never mention? Empty states, concurrent access, partial failure, the second
   time this runs (not just the first), scale beyond the happy-path example.

3. **Security gaps** — Anything touching credentials, user input, network
   boundaries, file paths, or privilege. Does the plan cross a trust boundary
   without saying how it's guarded? Would implementing this plan as literally
   written introduce a vulnerability?

4. **Scope creep** — Does the plan promise more than the interview actually
   established a need for? Flag anything that reads like a "nice to have" smuggled
   in as a requirement, and anything where the stated goal could be met with
   meaningfully less.

5. **Untested claims** — Any claim in the plan phrased as fact ("this will scale",
   "this is backwards compatible", "users won't notice") that isn't backed by
   something the interview or existing project memory actually established.
   Rewrite it as a risk or an open question instead of asserting it.

## What "changing the plan" looks like

A finding that only describes a problem is half the job. Where you can, also state
what the plan should carry as a result: a new risk entry, a new assumption made
explicit, or a scope line removed. A critique pass that never changes anything is
almost certainly not looking hard enough.

## What this is not

This is not a rewrite of the plan, and not a second interview. If a finding
reveals that a genuinely new clarifying question is needed, say that explicitly
as its own finding rather than inventing an answer on the plan's behalf.
