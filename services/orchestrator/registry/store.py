"""Loads and saves `projects.yaml` -- a top-level YAML list of ProjectRecords."""

from pathlib import Path

import yaml
from pydantic import TypeAdapter

from services.orchestrator.registry.schemas import ProjectRecord

_REGISTRY_ADAPTER = TypeAdapter(list[ProjectRecord])


def load_registry(path: Path) -> list[ProjectRecord]:
    """Loads the project registry.

    Args:
        path: Path to the projects.yaml file.

    Returns:
        The registered projects, or an empty list if path doesn't exist yet
        (a fresh deployment has no projects registered).

    Raises:
        pydantic.ValidationError: If the file's contents don't match ProjectRecord.
    """
    if not path.exists():
        return []
    raw = yaml.safe_load(path.read_text()) or []
    return _REGISTRY_ADAPTER.validate_python(raw)


def save_registry(path: Path, records: list[ProjectRecord]) -> None:
    """Writes the project registry as a top-level YAML list.

    Args:
        path: Path to the projects.yaml file. Parent directories are
            created if missing.
        records: The full set of registered projects to write.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = _REGISTRY_ADAPTER.dump_python(records, mode="json")
    path.write_text(yaml.safe_dump(raw, sort_keys=False))
