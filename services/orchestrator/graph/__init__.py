"""LangGraph nodes, state schema, checkpointer, and graph assembly.

Deliberately no re-exports here (ADR-030's fix, applied to this package
too): importing any submodule of this package always executes this file
first, and eagerly importing build.py/checkpointer.py would force
langgraph as a transitive dependency onto submodules that don't need it
(cost_aggregator.py, state.py's pure-dataclass pieces). Nothing in this
codebase imports from the package root -- confirmed via grep before this
file was stripped.
"""
