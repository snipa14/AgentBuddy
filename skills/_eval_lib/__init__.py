"""Shared, standalone eval harness for verifying skills/*/SKILL.md files.

Deliberately independent of services.orchestrator.model_router and
config/models.yaml -- per ADR-014, skills must work identically whether the
executing model is local, Claude API, or a harness outside Foundry
entirely, so their evals must not require Foundry's own routing config.
"""
