"""Coder agent (ARCHITECTURE.md §4.7, §6.2).

model_router's CompletionRequest/CompletionResult (services/orchestrator/
model_router/schemas.py) is a plain text-completion contract with no native
tool-calling support, so the Coder drives its own manual ReAct loop instead
of relying on a provider's built-in tool-use: `decide_next_action` asks the
model for exactly one structured action per turn, the loop dispatches it to
the Executor via `ExecutorClient`, and the observation (file contents,
command output, or an error) becomes the next turn's context. This mirrors
Planner's single-call, structured-text-response shape (services/
orchestrator/agents/planner.py) and shares its parsing helpers (agents/
parsing.py).

`decompose_plan_into_tasks` and `run_coder_task` are both plain, testable
async functions with no LangGraph or real-Docker dependency -- they're
exercised against a FakeRouter and a RecordingExecutorClient in
tests/unit/test_coder.py. The real end-to-end proof against a live
workspace container lives in tests/integration/test_coder_end_to_end.py.
"""

from typing import Literal

from pydantic import BaseModel

from services.orchestrator.agents.parsing import nonempty_lines, split_sections
from services.orchestrator.executor_client import ExecutorClient, ExecutorClientError
from services.orchestrator.model_router.router import ModelRouter
from services.orchestrator.model_router.schemas import CompletionMessage, CompletionRequest

CODER_ROLE = "coder"

ActionKind = Literal["read_file", "write_file", "run_command", "done"]


class CoderTask(BaseModel):
    """One item from a decomposed plan's task_queue."""

    description: str


class CoderAction(BaseModel):
    """One turn's decision in the Coder's manual ReAct loop."""

    kind: ActionKind
    path: str | None = None
    content: str | None = None
    command: list[str] | None = None


class CoderTaskResult(BaseModel):
    """The outcome of running a single task to completion or exhausting max_steps."""

    success: bool
    transcript: list[str]


_DECOMPOSE_SYSTEM_PROMPT = (
    "You are breaking a software project plan into an ordered list of small, "
    "concrete implementation tasks. Respond with '## Tasks' followed by one "
    "task per line, each a short imperative description (e.g. 'Add a User "
    "model with email and password_hash fields')."
)

_CODER_SYSTEM_PROMPT = (
    "You are implementing one task inside a project's workspace container. "
    "You have three tools: read_file, write_file, run_command. Respond with "
    "exactly one action per turn: '## Action' followed by one of "
    "'read_file', 'write_file', 'run_command', or 'done', then whatever "
    "additional sections that action needs: '## Path' for read_file; "
    "'## Path' and '## Content' for write_file; '## Command' (one argv "
    "token per line) for run_command. Respond with just '## Action\\ndone' "
    "once the task is fully implemented and verified."
)


async def decompose_plan_into_tasks(router: ModelRouter, plan: dict) -> list[CoderTask]:
    """Breaks an approved plan into an ordered list of concrete implementation tasks.

    Args:
        router: A ModelRouter to resolve the "coder" role from.
        plan: The approved plan dict (a DraftPlan.model_dump(), per
            services.orchestrator.agents.planner).

    Returns:
        An ordered list of CoderTasks to feed into ProjectState.task_queue.

    Raises:
        NoCapableProviderError: If no provider is configured for "coder".
        ProviderRequestError: If the underlying model call fails.
        ValueError: If the model's response contains no tasks.
    """
    provider = router.resolve(CODER_ROLE)
    plan_text = (
        f"Summary: {plan.get('summary', '')}\n"
        f"Goals: {plan.get('goals', [])}\n"
        f"Milestones: {plan.get('milestones', [])}"
    )
    request = CompletionRequest(
        messages=[
            CompletionMessage(role="system", content=_DECOMPOSE_SYSTEM_PROMPT),
            CompletionMessage(role="user", content=plan_text),
        ]
    )
    result = await provider.complete(request)
    sections = split_sections(result.content)
    descriptions = nonempty_lines(sections.get("Tasks", ""))
    if not descriptions:
        raise ValueError(f"malformed task decomposition, no tasks found: {result.content!r}")
    return [CoderTask(description=description) for description in descriptions]


async def decide_next_action(
    router: ModelRouter, task_description: str, transcript: list[str]
) -> CoderAction:
    """Asks the Coder for its next action on the current task.

    Args:
        router: A ModelRouter to resolve the "coder" role from.
        task_description: The current task's description.
        transcript: Prior "Action: ... -> observation" entries for this task.

    Returns:
        The next CoderAction to perform.

    Raises:
        NoCapableProviderError: If no provider is configured for "coder".
        ProviderRequestError: If the underlying model call fails.
        ValueError: If the model's response doesn't match the expected format.
    """
    provider = router.resolve(CODER_ROLE)
    request = CompletionRequest(
        messages=[
            CompletionMessage(role="system", content=_CODER_SYSTEM_PROMPT),
            CompletionMessage(
                role="user",
                content=f"Task: {task_description}\n\nTranscript so far:\n"
                + "\n".join(transcript),
            ),
        ]
    )
    result = await provider.complete(request)
    return _parse_coder_action(result.content)


async def run_coder_task(
    router: ModelRouter,
    executor_client: ExecutorClient,
    project_id: str,
    task_description: str,
    max_steps: int = 10,
) -> CoderTaskResult:
    """Runs a single task to completion (or exhausts max_steps) via the ReAct loop.

    Args:
        router: A ModelRouter to resolve the "coder" role from.
        executor_client: The Executor client every read_file/write_file/
            run_command action is dispatched through.
        project_id: The project whose workspace this task operates in.
        task_description: The task to implement.
        max_steps: Safety cap on actions taken before giving up.

    Returns:
        A CoderTaskResult: success=True once the Coder emits "done";
        success=False if max_steps is exhausted first. transcript records
        every action taken and its observation either way, for the graph
        node's retry bookkeeping and for tests to assert on (not just final
        state).
    """
    transcript: list[str] = []
    for _ in range(max_steps):
        action = await decide_next_action(router, task_description, transcript)
        if action.kind == "done":
            return CoderTaskResult(success=True, transcript=transcript)
        observation = await _perform_action(executor_client, project_id, action)
        transcript.append(f"Action: {action.kind} -> {observation}")
    return CoderTaskResult(success=False, transcript=transcript)


async def _perform_action(
    executor_client: ExecutorClient, project_id: str, action: CoderAction
) -> str:
    """Dispatches one CoderAction to the Executor, returning a text observation.

    Executor errors are caught and turned into an observation rather than
    raised, so the Coder's own next turn can see what went wrong and try a
    different approach within the same task attempt, before the graph
    node's outer retry budget is ever spent.
    """
    try:
        if action.kind == "read_file":
            content = await executor_client.read_file(project_id, action.path or "")
            return f"contents of {action.path}:\n{content}"
        if action.kind == "write_file":
            await executor_client.write_file(project_id, action.path or "", action.content or "")
            return f"wrote {action.path}"
        if action.kind == "run_command":
            result = await executor_client.run_command(project_id, action.command or [])
            return (
                f"exit_code={result.exit_code}\n"
                f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
            )
    except ExecutorClientError as exc:
        return f"error: {exc}"
    raise ValueError(f"unknown action kind: {action.kind!r}")


def _parse_coder_action(content: str) -> CoderAction:
    """Parses one '## Action' / '## Path' / '## Content' / '## Command' response.

    Raises:
        ValueError: If '## Action' is missing or isn't a known action kind.
    """
    sections = split_sections(content)
    kind = sections.get("Action", "").strip()
    if kind not in ("read_file", "write_file", "run_command", "done"):
        raise ValueError(f"malformed coder action: {content!r}")
    if kind == "read_file":
        return CoderAction(kind=kind, path=sections.get("Path", "").strip())
    if kind == "write_file":
        return CoderAction(
            kind=kind,
            path=sections.get("Path", "").strip(),
            content=sections.get("Content", "").strip("\n"),
        )
    if kind == "run_command":
        return CoderAction(kind=kind, command=nonempty_lines(sections.get("Command", "")))
    return CoderAction(kind="done")
