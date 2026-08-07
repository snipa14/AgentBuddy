"""Deterministic static-check runner -- the no-LLM half of ADR-046.

Runs a stack's check_commands from config/stacks.yaml inside the project's
workspace container through the Executor's existing run_command tool. Makes
zero LLM calls by design: cheap deterministic verification happens before
any Reviewer token is spent, and this module is independently testable
without langgraph (the same layering as agents/tester.py).

Skip semantics keep the checks honest on projects that legitimately lack a
tool: a check whose requires_file is absent from the project is skipped
before it runs, and a check whose executable is missing from the workspace
image (exec exit codes 126/127) is skipped after -- neither is a failure.

A container/network blip during either Executor call (the requires_file
probe or the check itself) is a different case: it isn't evidence the tool
is missing or inapplicable, so it is recorded as a StaticCheckFailure
(exit_code=INFRA_FAILURE_EXIT_CODE) rather than silently skipped -- this
fails closed, matching how run_coder_task's tool wrappers surface Executor
errors as feedback instead of swallowing them, and lets the existing
reopen-last-done-task-or-block path in node_static_checks (graph/nodes.py)
give the Coder a chance to retry a transient failure, escalating to
"blocked" only if it doesn't clear within MAX_TASK_ATTEMPTS. Both an
ExecutorClientError (the Executor reachable but returning a non-2xx
response) and an httpx.RequestError (the Executor unreachable at all --
connection refused, DNS failure, timeout, the more literal reading of
"network blip") are treated this way.
"""

import logging

import httpx
from pydantic import BaseModel, Field

from services.orchestrator.executor_client import ExecutorClientError
from services.orchestrator.stacks import StacksConfig

logger = logging.getLogger(__name__)

TOOL_MISSING_EXIT_CODES = frozenset({126, 127})
INFRA_FAILURE_EXIT_CODE = -1
"""Sentinel StaticCheckFailure.exit_code for a container/network failure that
prevented a check from running at all, distinguishable from any real process
exit code (0-255)."""


class StaticCheckFailure(BaseModel):
    """One failed check command and its raw output.

    Attributes:
        command: The argv list that failed.
        exit_code: The command's exit code.
        output: Combined stdout + stderr, verbatim -- this is the feedback
            the Coder gets, so it is never summarized or truncated here.
    """

    command: list[str]
    exit_code: int
    output: str


class StaticCheckOutcome(BaseModel):
    """The result of running every applicable check for a project.

    Attributes:
        ran: Commands that executed to completion (pass or fail).
        skipped: Human-readable "command -- reason" entries for checks that
            did not apply.
        failures: Every check that ran and exited non-zero.
    """

    ran: list[list[str]] = Field(default_factory=list)
    skipped: list[str] = Field(default_factory=list)
    failures: list[StaticCheckFailure] = Field(default_factory=list)

    @property
    def passed(self) -> bool:
        """True when no applicable check failed."""
        return not self.failures


async def _run_or_record_infra_failure(
    executor_client,
    project_id: str,
    stack: str,
    argv: list[str],
    record_command: list[str],
    context: str,
    outcome: StaticCheckOutcome,
):
    """Runs argv through the Executor, converting an infra failure to a StaticCheckFailure.

    Shared by both Executor calls in run_static_checks (the requires_file
    probe and the check command itself) -- each wants the same fail-closed
    treatment of an infra failure, differing only in which argv actually
    runs, which command gets attributed to the resulting StaticCheckFailure
    (always the check's own command, even when argv is the probe's), and the
    text describing what was being attempted.

    Args:
        executor_client: The Executor client to run argv through.
        project_id: The project whose workspace container to use.
        stack: The project's declared stack.
        argv: The command to actually execute.
        record_command: The command attributed to the StaticCheckFailure on
            failure -- the check's own command, not necessarily argv.
        context: Human-readable description of what argv is doing (e.g.
            "probing for tsconfig.json" or "running ruff check ."), used in
            both the warning log and the recorded failure's output.
        outcome: The in-progress StaticCheckOutcome; a failure is appended
            to it directly so callers don't have to.

    Returns:
        The Executor's CommandResult on success, or None if an
        ExecutorClientError (non-2xx response) or an httpx.RequestError
        (connection failure, timeout -- a genuine network blip, as opposed
        to a live Executor returning an error) was raised and recorded.
    """
    try:
        return await executor_client.run_command(project_id, argv, stack=stack)
    except (ExecutorClientError, httpx.RequestError) as exc:
        logger.warning(
            "project %s: executor infra failure %s: %s", project_id, context, exc
        )
        outcome.failures.append(
            StaticCheckFailure(
                command=list(record_command),
                exit_code=INFRA_FAILURE_EXIT_CODE,
                output=f"executor infra failure {context}: {exc}",
            )
        )
        return None


async def run_static_checks(
    executor_client,
    project_id: str,
    stack: str,
    stacks_config: StacksConfig,
) -> StaticCheckOutcome:
    """Runs a stack's deterministic checks inside the workspace container.

    Args:
        executor_client: The Executor client checks are dispatched through
            (duck-typed to ExecutorClient.run_command, so tests can stub it).
        project_id: The project whose workspace container to use.
        stack: The project's declared stack -- selects both the workspace
            image (via the Executor) and which check_commands apply.
        stacks_config: The loaded stack registry (ADR-045).

    Returns:
        A StaticCheckOutcome; `.passed` is True when nothing applicable
        failed, including when the stack has no registry entry or no
        check_commands at all. A container/network failure from the
        Executor while probing requires_file or running a check's command
        is recorded as a StaticCheckFailure with
        exit_code=INFRA_FAILURE_EXIT_CODE rather than raised, so this
        function always returns cleanly -- neither an ExecutorClientError
        (a live Executor returning a non-2xx response) nor an
        httpx.RequestError (a connection failure or timeout reaching the
        Executor at all) propagates out of it.
    """
    outcome = StaticCheckOutcome()
    entry = stacks_config.stacks.get(stack)
    if entry is None or not entry.check_commands:
        logger.info(
            "project %s: no static checks registered for stack %r",
            project_id,
            stack,
        )
        return outcome

    for check in entry.check_commands:
        rendered = " ".join(check.command)
        if check.requires_file is not None:
            probe = await _run_or_record_infra_failure(
                executor_client,
                project_id,
                stack,
                ["test", "-f", check.requires_file],
                check.command,
                f"probing for {check.requires_file}",
                outcome,
            )
            if probe is None:
                continue
            if probe.exit_code != 0:
                outcome.skipped.append(
                    f"{rendered} -- requires {check.requires_file}, "
                    "not present"
                )
                logger.info(
                    "project %s: skipping %s (%s absent)",
                    project_id,
                    rendered,
                    check.requires_file,
                )
                continue

        result = await _run_or_record_infra_failure(
            executor_client,
            project_id,
            stack,
            check.command,
            check.command,
            f"running {rendered}",
            outcome,
        )
        if result is None:
            continue

        if result.exit_code in TOOL_MISSING_EXIT_CODES:
            outcome.skipped.append(
                f"{rendered} -- tool missing (exit {result.exit_code})"
            )
            logger.info(
                "project %s: skipping %s (tool missing)", project_id, rendered
            )
            continue

        outcome.ran.append(list(check.command))
        if result.exit_code != 0:
            outcome.failures.append(
                StaticCheckFailure(
                    command=list(check.command),
                    exit_code=result.exit_code,
                    output=f"{result.stdout}\n{result.stderr}".strip(),
                )
            )

    logger.info(
        "project %s: static checks ran=%d skipped=%d failed=%d",
        project_id,
        len(outcome.ran),
        len(outcome.skipped),
        len(outcome.failures),
    )
    return outcome
