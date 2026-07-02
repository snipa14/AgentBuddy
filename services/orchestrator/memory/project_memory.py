"""Per-project markdown memory files (ARCHITECTURE.md §4.5).

Git-tracked and human-readable, living under `projects/<project_id>/memory/`
on the same volume the Orchestrator mounts at `/data/projects`
(docker-compose.yml). Only appends are supported -- never full-file
rewrites -- so a small addition stays a single-line diff in git.
"""

from pathlib import Path

MEMORY_FILENAMES = ("PROJECT_MEMORY.md", "DECISIONS.md", "LEARNINGS.md")


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
    text = entry if entry.endswith("\n") else entry + "\n"
    with (directory / filename).open("a", encoding="utf-8") as handle:
        handle.write(text)


def _validate_filename(filename: str) -> None:
    if filename not in MEMORY_FILENAMES:
        raise ValueError(f"unknown memory file {filename!r}, expected one of {MEMORY_FILENAMES}")
