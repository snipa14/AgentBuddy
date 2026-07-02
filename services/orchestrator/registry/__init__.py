"""Project registry: projects.yaml schema + registration (ARCHITECTURE.md §4.6, ADR-013).

Deliberately does not import `cli`: unlike this package's other modules,
`cli.main()`'s intake-starting hook needs LangGraph, so it's kept out of
this package's eagerly-imported surface for the same reason Phase 6 removed
the top-level `services.orchestrator` package's eager re-export.
"""

from services.orchestrator.registry.errors import (
    BrownfieldSourceError,
    DuplicateProjectIdError,
    InvalidProjectIdError,
    RegistryError,
)
from services.orchestrator.registry.registration import (
    register_brownfield_from_git,
    register_brownfield_from_local_path,
    register_greenfield,
)
from services.orchestrator.registry.schemas import ProjectRecord
from services.orchestrator.registry.store import load_registry, save_registry

__all__ = [
    "BrownfieldSourceError",
    "DuplicateProjectIdError",
    "InvalidProjectIdError",
    "ProjectRecord",
    "RegistryError",
    "load_registry",
    "register_brownfield_from_git",
    "register_brownfield_from_local_path",
    "register_greenfield",
    "save_registry",
]
