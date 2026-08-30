"""`projects`/`rename` CLI entrypoints (ARCHITECTURE.md §4.6, ADR-055).

Registry-only operations -- unlike `registry.cli`'s `new-project`, these
never start the intake graph, so this module has no LangGraph dependency at
all and stays importable/testable in any environment. `./foundry projects`
and `./foundry rename` shell out to this module the same way `./foundry
new-project` shells out to `registry.cli`.
"""

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from services.orchestrator.registry.errors import DuplicateNameError
from services.orchestrator.registry.schemas import ProjectRecord
from services.orchestrator.registry.store import load_registry, save_registry

_raw_projects_root = os.environ.get("PROJECTS_ROOT")
DEFAULT_PROJECTS_ROOT = Path(_raw_projects_root) if _raw_projects_root else Path("/data/projects")
"""Mirrors `registry.cli.DEFAULT_PROJECTS_ROOT`'s resolution exactly -- see
that module's docstring for why this must stay in sync with
`OrchestratorConfig.from_env`'s `PROJECTS_ROOT` resolution."""
DEFAULT_REGISTRY_PATH = DEFAULT_PROJECTS_ROOT / "projects.yaml"


def build_parser() -> argparse.ArgumentParser:
    """Builds the `projects`/`rename` argument parser.

    Returns:
        An argparse.ArgumentParser with a required `command` subparser
        ("projects" or "rename").
    """
    parser = argparse.ArgumentParser(prog="foundry")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("projects", help="List every registered project")
    rename_parser = subparsers.add_parser("rename", help="Rename a registered project")
    rename_parser.add_argument("project", help="The project's id or current registered name")
    rename_parser.add_argument("new_name", help="The new human-readable name")
    return parser


def _resolve(records: Sequence[ProjectRecord], identifier: str) -> ProjectRecord | None:
    """Finds the record matching identifier by exact id or case-insensitive name."""
    lowered = identifier.lower()
    for record in records:
        if record.id == identifier or record.name.lower() == lowered:
            return record
    return None


def rename_project(registry_path: Path, identifier: str, new_name: str) -> ProjectRecord:
    """Renames a registered project's entry in `projects.yaml`.

    Args:
        registry_path: Path to the projects.yaml registry file.
        identifier: The project's id, or its current registered name
            (case-insensitive).
        new_name: The new human-readable name.

    Returns:
        The updated ProjectRecord.

    Raises:
        LookupError: If identifier matches no registered project.
        DuplicateNameError: If new_name is already used by a different
            registered project (case-insensitive).
    """
    records = load_registry(registry_path)
    target = _resolve(records, identifier)
    if target is None:
        raise LookupError(identifier)
    if any(
        record.id != target.id and record.name.lower() == new_name.lower() for record in records
    ):
        raise DuplicateNameError(f"{new_name!r} is already used by another project")
    updated = target.model_copy(update={"name": new_name})
    save_registry(
        registry_path,
        [updated if record.id == target.id else record for record in records],
    )
    return updated


def _print_projects(registry_path: Path) -> int:
    records = load_registry(registry_path)
    if not records:
        print("No projects registered yet. Register one with `foundry new-project`.")
        return 0
    for record in sorted(records, key=lambda record: record.id):
        print(f"{record.id}\t{record.name}\t{record.stack}\t{record.status}")
    return 0


def _rename(registry_path: Path, identifier: str, new_name: str) -> int:
    try:
        updated = rename_project(registry_path, identifier, new_name)
    except LookupError:
        print(f"rename: unknown project {identifier!r}", file=sys.stderr)
        return 1
    except DuplicateNameError as exc:
        print(f"rename: {exc}", file=sys.stderr)
        return 1
    print(f"renamed {updated.id!r} to {updated.name!r}")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Parses arguments and runs the requested registry command.

    Args:
        argv: Command-line arguments, excluding the program name. Defaults
            to `sys.argv[1:]` when None.

    Returns:
        0 on success, 1 on a handled error (unknown project, duplicate name).
    """
    args = build_parser().parse_args(argv)
    if args.command == "projects":
        return _print_projects(DEFAULT_REGISTRY_PATH)
    return _rename(DEFAULT_REGISTRY_PATH, args.project, args.new_name)


if __name__ == "__main__":
    raise SystemExit(main())
