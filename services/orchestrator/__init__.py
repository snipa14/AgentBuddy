"""Foundry orchestrator: LangGraph orchestration, agents, and model routing.

See `services.orchestrator.graph` for the per-project subgraph, `service`
for the routing layer atop it, `api` for the FastAPI surface the Telegram
Gateway calls (ARCHITECTURE.md §4.2), and `memory`/`agents` for the memory
system and specialist agents (§4.5, §4.7).

Deliberately does not re-export those submodules' public names here: unlike
`model_router` or `telegram_gateway`, this package is an umbrella over
several subsystems with very different dependency footprints (`memory`
needs only the standard library and pydantic; `graph`/`service`/`api` need
langgraph and fastapi). Eagerly importing the heaviest one at package level
would force every submodule import -- including ones with no LangGraph
dependency at all -- to require langgraph installed.
"""
