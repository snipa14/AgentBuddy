"""Project registration: on-disk structure + registry entry (ARCHITECTURE.md §4.6).

Every registration path accepts an optional `on_registered` hook, invoked
with the new project_id once the on-disk structure and registry entry are
committed -- this is how a caller wires in "start the intake graph"
(TODO.md Phase 7) without this module importing anything from
`services.orchestrator.graph`/`service`, which would pull in LangGraph.
The real wiring lives in `services.orchestrator.registry.cli`.
"""

import subprocess
from pathlib import Path
from typing import Callable

from services.orchestrator.memory.project_memory import MEMORY_FILENAMES, project_memory_dir
from services.orchestrator.registry.errors import BrownfieldSourceError, DuplicateProjectIdError
from services.orchestrator.registry.schemas import PrMode, ProjectRecord
from services.orchestrator.registry.store import load_registry, save_registry
from services.orchestrator.registry.validation import validate_project_id

OnRegistered = Callable[[str], None]


def register_greenfield(
    registry_path: Path,
    projects_root: Path,
    project_id: str,
    name: str,
    stack: str,
    coverage_threshold: int = 80,
    pr_mode: PrMode = "manual",
    cost_ceiling_usd_weekly: float | None = None,
    on_registered: OnRegistered | None = None,
) -> ProjectRecord:
    """Registers a brand-new greenfield project.

    Creates `projects/<project_id>/` with empty memory files.

    Args:
        registry_path: Path to the projects.yaml registry file.
        projects_root: The projects/ root directory.
        project_id: The new project's identifier.
        name: A human-readable project name.
        stack: The project's primary language/stack (e.g. "python").
        coverage_threshold: Minimum coverage percentage required to pass testing.
        pr_mode: How pull requests are handled for this project.
        cost_ceiling_usd_weekly: Per-project cost ceiling override, or None
            to use the global default.
        on_registered: Optional hook invoked with project_id once
            registration succeeds (e.g. to start the intake graph).

    Returns:
        The newly created ProjectRecord.

    Raises:
        InvalidProjectIdError: If project_id isn't a safe slug.
        DuplicateProjectIdError: If project_id is already registered.
    """
    records = _ensure_registrable(registry_path, project_id)

    project_dir = projects_root / project_id
    project_dir.mkdir(parents=True, exist_ok=True)
    _create_empty_memory_files(projects_root, project_id)

    record = ProjectRecord(
        id=project_id,
        name=name,
        mode="greenfield",
        path=str(project_dir),
        git_remote=None,
        stack=stack,
        coverage_threshold=coverage_threshold,
        pr_mode=pr_mode,
        cost_ceiling_usd_weekly=cost_ceiling_usd_weekly,
    )
    return _finalize_registration(registry_path, records, record, on_registered)


def register_brownfield_from_local_path(
    registry_path: Path,
    projects_root: Path,
    project_id: str,
    name: str,
    source_path: Path,
    stack: str,
    coverage_threshold: int = 80,
    pr_mode: PrMode = "manual",
    cost_ceiling_usd_weekly: float | None = None,
    on_registered: OnRegistered | None = None,
) -> ProjectRecord:
    """Registers a brownfield project by linking in an existing local directory.

    Args:
        registry_path: Path to the projects.yaml registry file.
        projects_root: The projects/ root directory.
        project_id: The new project's identifier.
        name: A human-readable project name.
        source_path: An existing local directory to link in under
            `projects/<project_id>` as a symlink.
        stack: The project's primary language/stack (e.g. "python").
        coverage_threshold: Minimum coverage percentage required to pass testing.
        pr_mode: How pull requests are handled for this project.
        cost_ceiling_usd_weekly: Per-project cost ceiling override, or None
            to use the global default.
        on_registered: Optional hook invoked with project_id once
            registration succeeds (e.g. to start the intake graph in
            "extend existing codebase" mode).

    Returns:
        The newly created ProjectRecord.

    Raises:
        InvalidProjectIdError: If project_id isn't a safe slug.
        DuplicateProjectIdError: If project_id is already registered.
        BrownfieldSourceError: If source_path doesn't exist or isn't a directory.
    """
    records = _ensure_registrable(registry_path, project_id)

    if not source_path.is_dir():
        raise BrownfieldSourceError(
            f"source path {source_path} does not exist or is not a directory"
        )

    project_dir = projects_root / project_id
    projects_root.mkdir(parents=True, exist_ok=True)
    project_dir.symlink_to(source_path.resolve(), target_is_directory=True)
    _create_empty_memory_files(projects_root, project_id)

    record = ProjectRecord(
        id=project_id,
        name=name,
        mode="brownfield",
        path=str(project_dir),
        git_remote=None,
        stack=stack,
        coverage_threshold=coverage_threshold,
        pr_mode=pr_mode,
        cost_ceiling_usd_weekly=cost_ceiling_usd_weekly,
    )
    return _finalize_registration(registry_path, records, record, on_registered)


def register_brownfield_from_git(
    registry_path: Path,
    projects_root: Path,
    project_id: str,
    name: str,
    git_remote: str,
    stack: str,
    coverage_threshold: int = 80,
    pr_mode: PrMode = "manual",
    cost_ceiling_usd_weekly: float | None = None,
    on_registered: OnRegistered | None = None,
) -> ProjectRecord:
    """Registers a brownfield project by cloning a git remote.

    Args:
        registry_path: Path to the projects.yaml registry file.
        projects_root: The projects/ root directory.
        project_id: The new project's identifier.
        name: A human-readable project name.
        git_remote: A git URL (or local path) to clone under `projects/<project_id>`.
        stack: The project's primary language/stack (e.g. "python").
        coverage_threshold: Minimum coverage percentage required to pass testing.
        pr_mode: How pull requests are handled for this project.
        cost_ceiling_usd_weekly: Per-project cost ceiling override, or None
            to use the global default.
        on_registered: Optional hook invoked with project_id once
            registration succeeds (e.g. to start the intake graph in
            "extend existing codebase" mode).

    Returns:
        The newly created ProjectRecord.

    Raises:
        InvalidProjectIdError: If project_id isn't a safe slug.
        DuplicateProjectIdError: If project_id is already registered.
        BrownfieldSourceError: If `git clone` fails.
    """
    records = _ensure_registrable(registry_path, project_id)

    project_dir = projects_root / project_id
    projects_root.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            ["git", "clone", "--quiet", git_remote, str(project_dir)],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        raise BrownfieldSourceError(
            f"git clone of {git_remote!r} failed: {exc.stderr.strip()}"
        ) from exc
    _create_empty_memory_files(projects_root, project_id)

    record = ProjectRecord(
        id=project_id,
        name=name,
        mode="brownfield",
        path=str(project_dir),
        git_remote=git_remote,
        stack=stack,
        coverage_threshold=coverage_threshold,
        pr_mode=pr_mode,
        cost_ceiling_usd_weekly=cost_ceiling_usd_weekly,
    )
    return _finalize_registration(registry_path, records, record, on_registered)


def _ensure_registrable(registry_path: Path, project_id: str) -> list[ProjectRecord]:
    """Validates project_id and returns the current registry if it's not a duplicate.

    Raises:
        InvalidProjectIdError: If project_id isn't a safe slug.
        DuplicateProjectIdError: If project_id is already registered.
    """
    validate_project_id(project_id)
    records = load_registry(registry_path)
    if any(record.id == project_id for record in records):
        raise DuplicateProjectIdError(f"project id {project_id!r} is already registered")
    return records


def _finalize_registration(
    registry_path: Path,
    records: list[ProjectRecord],
    record: ProjectRecord,
    on_registered: OnRegistered | None,
) -> ProjectRecord:
    records.append(record)
    save_registry(registry_path, records)
    if on_registered is not None:
        on_registered(record.id)
    return record


def _create_empty_memory_files(projects_root: Path, project_id: str) -> None:
    directory = project_memory_dir(projects_root, project_id)
    directory.mkdir(parents=True, exist_ok=True)
    for filename in MEMORY_FILENAMES:
        path = directory / filename
        if not path.exists():
            path.touch()
