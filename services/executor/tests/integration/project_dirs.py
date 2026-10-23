"""Shared helper for giving an integration test's project directory the permissions production has.

Every workspace container runs as the non-root UID in
`services.executor.containers.WORKSPACE_UID` (.claude/rules/security.md's
launch checklist), so any process inside it -- `npm install` creating
`node_modules/`, `go mod download` writing `go.sum` -- can only write into
the bind-mounted project directory if that directory's permissions allow
that UID.

Production solves this by chowning: the Orchestrator runs as root, creates
`projects/<project_id>/` root-owned at mode 755, and
`services.orchestrator.memory.project_memory.chown_to_workspace_uid` hands
it to the workspace UID immediately afterward. Tests cannot always do the
same -- `os.chown` to an arbitrary UID requires root, and a test process
usually is not root (GitHub Actions runs the job as the unprivileged
`runner` user). A chown-only helper silently no-ops there, leaving the
directory owned by the test user and unwritable by the container, which is
not a container-permission bug but a test-fixture one.

So this helper chowns when it can and otherwise widens the mode instead.
Widening is acceptable only because the target is a throwaway pytest tmp
directory: production deliberately does not loosen to world-writable (see
`chown_to_workspace_uid`'s docstring), and nothing here changes that -- the
container under test still runs non-root at WORKSPACE_UID, which is the
property these tests exist to prove.
"""

import os
import stat
from pathlib import Path

from services.executor.containers import WORKSPACE_UID


def make_writable_by_workspace_uid(path: Path) -> None:
    """Grants the workspace container's UID write access to *path*.

    Chowns *path* to WORKSPACE_UID when the calling process is root,
    reproducing production's exact ownership. Otherwise adds group and
    other write/execute bits, since a non-root process cannot chown to
    another UID but can still make its own directory writable by one.

    Args:
        path: The project directory to make writable.
    """
    if os.geteuid() == 0:
        os.chown(path, WORKSPACE_UID, WORKSPACE_UID)
        return
    current_mode = stat.S_IMODE(path.stat().st_mode)
    path.chmod(current_mode | 0o077)
