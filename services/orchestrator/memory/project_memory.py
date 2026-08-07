"""Per-project markdown memory files (ARCHITECTURE.md §4.5).

Git-tracked and human-readable, living under `projects/<project_id>/memory/`
on the same volume the Orchestrator mounts at `/data/projects`
(docker-compose.yml). Only appends are supported -- never full-file
rewrites -- so a small addition stays a single-line diff in git.
"""

import os
from pathlib import Path

MEMORY_FILENAMES = ("PROJECT_MEMORY.md", "DECISIONS.md", "LEARNINGS.md")

WORKSPACE_CONTAINER_UID = 1000
"""Must match services/executor/containers.py's WORKSPACE_UID -- the fixed,
non-root user every Executor-spawned workspace container runs as
(.claude/rules/security.md). The Orchestrator's own Dockerfile has no USER
directive, so this process runs as root, and anything that creates
projects/<project_id>/ (registry registration, or -- on a path that never
went through registration -- the Memory Curator's first-phase curation call)
leaves the project directory root-owned at 755 unless it chowns it. A
root-owned, non-group-writable directory blocks the non-root workspace
container from creating anything inside it -- create()/start() succeed, but
every write_file's `mkdir -p` fails with a permission error, observed in
production as every write_file call 500ing while read_file/run_command kept
succeeding. Chowning the top-level project directory (not its memory/
subdirectory, which the workspace container never writes to -- only the
Memory Curator does, from the Orchestrator's own root process) to this UID
fixes that without loosening it to world-writable. `chown_to_workspace_uid`
is only effectful when actually running as root (os.geteuid() == 0): a
non-root dev/test process can't chown to an arbitrary UID at all, and would
otherwise crash every caller with PermissionError."""


def chown_to_workspace_uid(path: Path) -> None:
    """Chowns *path* to the workspace container's fixed UID/GID, if running as root.

    Called both by `services.orchestrator.registry.registration` right after
    a project's directory is created (so the very first Coder task in a
    freshly-registered project can write to it) and by `append_memory_entry`
    below (so a project that reached this module by some other path is still
    covered). A no-op when the calling process isn't root, since only root
    can chown to an arbitrary UID.

    Args:
        path: The directory (or symlink to one) to chown. Symlinks are
            followed, matching `os.chown`'s default behavior.
    """
    if os.geteuid() == 0:
        os.chown(path, WORKSPACE_CONTAINER_UID, WORKSPACE_CONTAINER_UID)


def project_memory_dir(projects_root: Path, project_id: str) -> Path:
    """Returns the memory directory for a given project.

    Args:
        projects_root: The projects/ root directory.
        project_id: The project whose memory directory is being located.

    Returns:
        `projects_root / project_id / "memory"`, which may not exist yet.
    """
    return projects_root / project_id / "memory"


def read_memory_file(projects_root: Path, project_id: str, filename: str) -> str:
    """Reads one of a project's markdown memory files.

    Args:
        projects_root: The projects/ root directory.
        project_id: The project whose memory file is being read.
        filename: One of PROJECT_MEMORY.md, DECISIONS.md, LEARNINGS.md.

    Returns:
        The file's full contents, or "" if it hasn't been created yet.

    Raises:
        ValueError: If filename isn't one of the three known memory files.
    """
    _validate_filename(filename)
    path = project_memory_dir(projects_root, project_id) / filename
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def append_memory_entry(projects_root: Path, project_id: str, filename: str, entry: str) -> None:
    """Appends a single markdown entry to one of a project's memory files.

    Creates the project's memory directory and file on first use. Appends
    only -- never rewrites existing content.

    Args:
        projects_root: The projects/ root directory.
        project_id: The project whose memory file is being appended to.
        filename: One of PROJECT_MEMORY.md, DECISIONS.md, LEARNINGS.md.
        entry: The markdown text to append. A trailing newline is added if
            entry doesn't already end with one.

    Raises:
        ValueError: If filename isn't one of the three known memory files.
    """
    _validate_filename(filename)
    directory = project_memory_dir(projects_root, project_id)
    directory.mkdir(parents=True, exist_ok=True)
    chown_to_workspace_uid(projects_root / project_id)
    text = entry if entry.endswith("\n") else entry + "\n"
    with (directory / filename).open("a", encoding="utf-8") as handle:
        handle.write(text)


def _validate_filename(filename: str) -> None:
    if filename not in MEMORY_FILENAMES:
        raise ValueError(f"unknown memory file {filename!r}, expected one of {MEMORY_FILENAMES}")
