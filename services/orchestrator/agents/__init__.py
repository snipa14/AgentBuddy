"""Specialist agent modules, one per LangGraph node role (ARCHITECTURE.md §4.7).

Deliberately re-exports nothing -- importing services.orchestrator.agents.coder
(or any other LangChain-backed submodule) requires langchain_core to already
be installed, and re-exporting its symbols here would make that a transitive
requirement of importing *any* submodule of this package, including ones
with no LangChain dependency of their own (agents.tester, most of
agents.reviewer). This is the same class of bug Phase 6 fixed in
services/orchestrator/__init__.py for langgraph -- import the specific
submodule you need instead (e.g. `from services.orchestrator.agents.coder
import CoderTask`).
"""
