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
"""

import logging

from pydantic import BaseModel, Field

from services.orchestrator.stacks import StacksConfig

logger = logging.getLogger(__name__)

TOOL_MISSING_EXIT_CODES = frozenset({126, 127})


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
        check_commands at all.
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
            probe = await executor_client.run_command(
                project_id, ["test", "-f", check.requires_file], stack=stack
            )
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

        result = await executor_client.run_command(
            project_id, check.command, stack=stack
        )
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
