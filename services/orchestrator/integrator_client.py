"""Git and GitHub operations for the integrator node.

All git operations go through the Executor's ``run_command`` as an argv list
-- never a shell string -- since ``run_command`` execs directly inside the
workspace container with no shell involved (services.executor.containers.
exec_in_container passes the command straight to the Docker SDK's
``exec_run``); shell operators like ``&&`` are never interpreted. GitHub API
calls use PyGithub with a token read from the ``github_token`` Docker secret
file (docker-compose.yml mounts it at ``/run/secrets/github_token``, the same
mechanism main.py uses for ``claude_api_key``) at call time, not at import
time, so tests (and a real deployment reading secrets after process start)
always see the current value.

The client is intentionally stateless: every method takes the project ID and
current state it needs as parameters rather than caching anything. This makes
the class trivially testable with a mock executor client.
"""

import logging
from pathlib import Path

from github import Github
from github.PullRequest import PullRequest

from services.orchestrator.executor_client import ExecutorClient

logger = logging.getLogger(__name__)

DEFAULT_SECRETS_DIR = Path("/run/secrets")


class IntegratorClient:
    """Performs git operations via the Executor and GitHub API calls directly."""

    def __init__(self, executor_client: ExecutorClient, secrets_dir: Path | None = None):
        """Builds the client.

        Args:
            executor_client: The Executor client to run git commands through.
            secrets_dir: Directory containing Docker secret files. Defaults
                to /run/secrets (the real Docker Compose secrets mount);
                overridable so tests can point at a tmp_path instead.
        """
        self._executor = executor_client
        self._secrets_dir = secrets_dir if secrets_dir is not None else DEFAULT_SECRETS_DIR

    async def commit_and_push(
        self,
        project_id: str,
        branch_name: str | None = None,
        remote_url: str | None = None,
        message: str = "chore: merge to main",
    ) -> tuple[str, str]:
        """Commit all workspace changes and push to a feature branch.

        Initializes a git repo if needed, configures the user identity, adds all
        tracked files, commits with the given message, creates (or checks out) the
        target branch, and pushes it to ``remote_url``.

        The commit is created on ``branch_name`` (checked out *before*
        committing), never on main -- .claude/rules/git.md's "all work
        happens on a branch" applies to generated projects too. The push is
        a plain fast-forward push, never forced.

        Pushing requires the *project's own* git configuration to carry
        working credentials (an SSH deploy key or a credential helper
        configured in the cloned repo): workspace containers deliberately
        receive no Foundry-level secrets (.claude/rules/security.md -- the
        GitHub token must never reach an agent-reachable surface, and
        run_command inside the workspace is one). A remote without such
        credentials fails the push and leaves the local commit intact.

        Args:
            project_id: The project identifier used to address the Executor.
            branch_name: Feature-branch name. Auto-generated from ``project_id``
                when omitted.
            remote_url: GitHub remote URL (e.g. ``git@github.com:user/repo.git``).
                When None, changes are committed locally only -- no push is
                attempted.
            message: Git commit message.

        Returns:
            A tuple of ``(branch_name, remote_url)`` -- the branch that was
            committed to (and pushed, if a remote was given) and the remote
            it was pushed to (empty string when no remote is configured).
        """
        if branch_name is None:
            branch_name = f"feature/{project_id}"

        await self._executor.run_command(
            project_id, ["git", "config", "user.email", "foundry@localhost"]
        )
        await self._executor.run_command(project_id, ["git", "config", "user.name", "Foundry"])

        # `git init` on an already-initialized repo is a safe, idempotent
        # no-op (it doesn't touch HEAD or existing history), so there's no
        # need to detect whether one already exists first.
        await self._executor.run_command(project_id, ["git", "init", "-b", "main"])

        await self._executor.run_command(project_id, ["git", "add", "-A"])
        diff_result = await self._executor.run_command(
            project_id, ["git", "diff", "--cached", "--quiet"]
        )
        if diff_result.exit_code == 0:
            logger.info("project %s: nothing to commit, skipping", project_id)
            return branch_name, ""

        await self._executor.run_command(project_id, ["git", "checkout", "-B", branch_name])
        await self._executor.run_command(project_id, ["git", "commit", "-m", message])

        if not remote_url:
            logger.info("project %s: committed to %s (no remote)", project_id, branch_name)
            return branch_name, ""

        remote_add = await self._executor.run_command(
            project_id, ["git", "remote", "add", "origin", remote_url]
        )
        if remote_add.exit_code != 0:
            # "origin" already exists from a prior run -- point it at the
            # current remote_url instead of failing.
            await self._executor.run_command(
                project_id, ["git", "remote", "set-url", "origin", remote_url]
            )
        await self._executor.run_command(project_id, ["git", "push", "-u", "origin", branch_name])
        logger.info("project %s: pushed to %s/%s", project_id, remote_url, branch_name)
        return branch_name, remote_url

    async def create_pull_request(
        self,
        title: str,
        body: str,
        head_branch: str,
        base_branch: str = "main",
        remote_url: str | None = None,
    ) -> tuple[bool, str]:
        """Create a GitHub pull request via the GitHub API.

        Uses PyGithub with a token read from the ``github_token`` Docker
        secret file at call time. Returns a ``(success, detail)`` tuple -- on
        success ``detail`` is the PR URL; on failure it is an error message.

        Args:
            title: PR title.
            body: PR description/body.
            head_branch: The feature branch to merge from.
            base_branch: Target branch (default: ``main``).
            remote_url: GitHub remote URL, used to extract the owner/repo slug.

        Returns:
            A ``(success, detail)`` tuple. When successful, ``detail`` is the PR
            URL string. On failure it is a human-readable error message.
        """
        try:
            github_token = (self._secrets_dir / "github_token").read_text().strip()
        except FileNotFoundError:
            github_token = ""
        if not github_token:
            logger.warning("no github_token secret configured, skipping PR creation")
            return False, "github_token secret not configured"

        repo_slug = self._extract_repo_slug(remote_url or "")
        if not repo_slug:
            logger.warning("cannot extract owner/repo from remote URL '%s'", remote_url)
            return False, f"Cannot determine repository from remote URL: {remote_url}"

        try:
            gh = Github(github_token)
            repo = gh.get_repo(repo_slug)
            pr: PullRequest = repo.create_pull(
                title=title,
                body=body,
                head=head_branch,
                base=base_branch,
            )
            logger.info("project %s: PR #%d created — %s", repo_slug, pr.number, pr.html_url)
            return True, pr.html_url
        except Exception as exc:
            logger.error("project %s: PR creation failed — %s", repo_slug, exc)
            return False, str(exc)

    @staticmethod
    def _extract_repo_slug(remote_url: str) -> str | None:
        """Derive ``owner/repo`` from a GitHub remote URL.

        Handles SSH-style (``git@github.com:owner/repo.git``) and HTTPS-style
        (``https://github.com/owner/repo.git``) remotes. Returns ``None`` when
        the URL does not look like a GitHub remote.
        """
        if not remote_url:
            return None

        for pattern in ("git@github.com:", "https://github.com/"):
            if pattern in remote_url:
                slug = remote_url.replace(pattern, "")
                slug = slug.rstrip("/").removesuffix(".git").strip()
                parts = slug.split("/")
                if len(parts) == 2:
                    return f"{parts[0]}/{parts[1]}"

        return None
