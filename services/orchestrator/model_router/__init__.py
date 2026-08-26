"""Capability-based agent-role -> LLM provider resolution.

See docs/model-router.md for usage and config/models.yaml for the default
provider/role configuration.

Deliberately re-exports nothing -- importing services.orchestrator.model_router.router
or services.orchestrator.model_router.config (which depend on langchain_core, pyyaml,
and langchain_anthropic/ollama/openai) requires those packages to already be installed.
Re-exporting their symbols here would make that a transitive requirement of importing
*any* submodule of this package, including ones with no such dependency of their own
(e.g. model_router.capabilities). This is the same class of bug ADR-030 fixed in
services/orchestrator.agents -- import the specific submodule you need instead
(e.g. `from services.orchestrator.model_router.router import ModelRouter`,
`from services.orchestrator.model_router.capabilities import Capability`).
"""
