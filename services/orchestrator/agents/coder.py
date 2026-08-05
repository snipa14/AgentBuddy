"""Coder agent (ARCHITECTURE.md §4.7, §6.2).

`run_coder_task` drives the model's native tool-calling loop (ADR-025):
`read_file`/`write_file`/`run_command` are bound as LangChain `@tool`
functions closed over a specific project's ExecutorClient, and the loop
dispatches whatever tool calls the model requests each turn. Task
completion is signaled by the model returning a turn with no further tool
calls, rather than a parsed "done" action string.

Each turn is streamed (`astream`, ADR-027) rather than a single `ainvoke`
call -- partial content is relayed into `progress_buffer` as it arrives, so
the Telegram Gateway's poller has something to show before a long turn
finishes, while the accumulated `AIMessageChunk`s are summed back into one
`AIMessage` for tool-dispatch and cost accounting exactly as `ainvoke` would
have returned.

`decompose_plan_into_tasks` and `run_coder_task` are both plain, testable
async functions with no LangGraph or real-Docker dependency -- they're
exercised against a FakeRouter and a RecordingExecutorClient in
tests/unit/test_coder.py. The real end-to-end proof against a live
workspace container lives in tests/integration/test_coder_end_to_end.py.
"""

import logging

from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.tools import BaseTool, tool
from pydantic import BaseModel

from services.orchestrator.agents.llm import invoke_structured
from services.orchestrator.executor_client import ExecutorClient, ExecutorClientError
from services.orchestrator.model_router.router import ModelRouter
from services.orchestrator.progress_buffer import add_progress

CODER_ROLE = "coder"
logger = logging.getLogger(__name__)


class CoderTask(BaseModel):
    """One item from a decomposed plan's task_queue."""

    description: str


class _TaskList(BaseModel):
    """Schema wrapper so the task decomposition call has a single structured-output root."""

    tasks: list[CoderTask]


class CoderTaskResult(BaseModel):
    """The outcome of running a single task to completion or exhausting max_steps."""

    success: bool
    transcript: list[str]
    written_files: list[str] = []
    cost_usd: float = 0.0


_DECOMPOSE_SYSTEM_PROMPT = (
    "You are breaking a software project plan into an ordered list of small, "
    "concrete implementation tasks, each a short imperative description "
    "(e.g. 'Add a User model with email and password_hash fields')."
)

_CODER_SYSTEM_PROMPT = (
    "You are implementing one task inside a project's workspace container. "
    "Use the read_file/write_file/run_command tools as needed to implement "
    "and verify the task. Once it is fully implemented and verified, "
    "respond with no further tool calls."
)


def _build_tools(executor_client: ExecutorClient, project_id: str, stack: str) -> list[BaseTool]:
    """Builds the Coder's three tools, bound to one project's workspace.

    Executor errors are caught and turned into a tool observation rather
    than raised, so the model's own next turn can see what went wrong and
    try a different approach within the same task attempt, before the
    graph node's outer retry budget is ever spent.

    Args:
        executor_client: The Executor client every tool call dispatches through.
        project_id: The project whose workspace these tools operate in.
        stack: The project's declared stack (ADR-028) -- selects the
            Executor's workspace image and cache volume for every call.
    """

    @tool
    async def read_file(path: str) -> str:
        """Reads a file's contents from the project workspace."""
        add_progress(project_id, f"→ read_file({path})")
        logger.info("project %s: read_file(%s)", project_id, path)
        try:
            content = await executor_client.read_file(project_id, path, stack=stack)
        except ExecutorClientError as exc:
            add_progress(project_id, f"✗ read_file({path}) failed: {exc}")
            logger.warning("project %s: read_file(%s) failed: %s", project_id, path, exc)
            return f"error: {exc}"
        add_progress(project_id, f"✓ read {path}")
        return content

    @tool
    async def write_file(path: str, content: str) -> str:
        """Writes content to a file in the project workspace, creating parent directories as needed."""
        add_progress(project_id, f"→ write_file({path})")
        logger.info("project %s: write_file(%s)", project_id, path)
        try:
            await executor_client.write_file(project_id, path, content, stack=stack)
        except ExecutorClientError as exc:
            add_progress(project_id, f"✗ write_file({path}) failed: {exc}")
            logger.warning("project %s: write_file(%s) failed: %s", project_id, path, exc)
            return f"error: {exc}"
        add_progress(project_id, f"✓ wrote {path}")
        return f"wrote {path}"

    @tool
    async def run_command(command: list[str]) -> str:
        """Runs a command inside the project workspace container, given as an argv list, e.g. ["pytest", "-q"]."""
        command_text = " ".join(command)
        add_progress(project_id, f"→ run_command({command_text})")
        logger.info("project %s: run_command(%s)", project_id, command_text)
        try:
            result = await executor_client.run_command(project_id, command, stack=stack)
        except ExecutorClientError as exc:
            add_progress(project_id, f"✗ run_command({command_text}) failed: {exc}")
            logger.warning(
                "project %s: run_command(%s) failed: %s", project_id, command_text, exc
            )
            return f"error: {exc}"
        add_progress(project_id, f"✓ {command_text} (exit {result.exit_code})")
        return f"exit_code={result.exit_code}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"

    return [read_file, write_file, run_command]


async def decompose_plan_into_tasks(
    router: ModelRouter,
    plan: dict,
    completed_task_descriptions: list[str] | None = None,
) -> tuple[list[CoderTask], AIMessage]:
    """Breaks an approved plan into an ordered list of concrete implementation tasks.

    Args:
        router: A ModelRouter to resolve the "coder" role from.
        plan: The approved plan dict (a DraftPlan.model_dump(), per
            services.orchestrator.agents.planner).
        completed_task_descriptions: Descriptions of tasks already finished
            on a prior pass, e.g. before a replan (ADR-047). When non-empty,
            appended to the prompt as a block the model must not re-plan.
            None or empty leaves the prompt unchanged from before this
            parameter existed.

    Returns:
        A tuple of (ordered CoderTasks, raw AIMessage) -- the raw message
        carries usage_metadata for the calling node to price via
        ModelRouter.cost_usd().

    Raises:
        NoCapableProviderError: If no provider is configured for "coder".
        ValueError: If the model's response contains no tasks.
    """
    llm = router.resolve(CODER_ROLE)
    plan_text = (
        f"Summary: {plan.get('summary', '')}\n"
        f"Goals: {plan.get('goals', [])}\n"
        f"Milestones: {plan.get('milestones', [])}"
    )
    if completed_task_descriptions:
        completed_bullets = "\n".join(f"- {desc}" for desc in completed_task_descriptions)
        plan_text += (
            "\n\nThe following tasks are already complete and must not be "
            f"re-planned:\n{completed_bullets}"
        )
    messages = [
        SystemMessage(content=_DECOMPOSE_SYSTEM_PROMPT),
        HumanMessage(content=plan_text),
    ]
    result, ai_message = await invoke_structured(llm, _TaskList, messages)
    if not result.tasks:
        raise ValueError(f"malformed task decomposition, no tasks found: {plan_text!r}")
    return result.tasks, ai_message


async def run_coder_task(
    router: ModelRouter,
    executor_client: ExecutorClient,
    project_id: str,
    task_description: str,
    max_steps: int = 10,
    stack: str = "python",
    recalled_lessons: list[str] | None = None,
) -> CoderTaskResult:
    """Runs a single task to completion (or exhausts max_steps) via native tool-calling.

    Args:
        router: A ModelRouter to resolve the "coder" role from.
        executor_client: The Executor client every read_file/write_file/
            run_command tool call is dispatched through.
        project_id: The project whose workspace this task operates in.
        task_description: The task to implement.
        max_steps: Safety cap on model turns before giving up.
        stack: The project's declared stack (ADR-028) -- selects the
            Executor's workspace image and cache volume for every tool call.
        recalled_lessons: Cross-project lessons from the global memory store
            (memory/global_store.recall_for_task), appended to the task
            prompt when non-empty so a fix recorded on one project actually
            reaches the next project's Coder (TODO.md Phase 14's goal --
            previously the store was write-only in production).

    Returns:
        A CoderTaskResult: success=True once the model returns a turn with no
        further tool calls; success=False if max_steps is exhausted first.
        transcript records every tool call and its observation either way,
        for the graph node's retry bookkeeping and for tests to assert on
        (not just final state). written_files collects every path touched by
        a write_file tool call during the run, in call order, regardless of
        whether the overall task succeeded. cost_usd accumulates every turn's
        completion cost (ModelRouter.cost_usd()), for the calling node to fold
        into ProjectState's running totals.
    """
    llm = router.resolve(CODER_ROLE)
    tools = _build_tools(executor_client, project_id, stack)
    llm_with_tools = llm.bind_tools(tools)
    tools_by_name = {tool_fn.name: tool_fn for tool_fn in tools}

    task_prompt = f"Task: {task_description}"
    if recalled_lessons:
        lessons_text = "\n".join(f"- {lesson}" for lesson in recalled_lessons)
        task_prompt += (
            f"\n\nLessons recorded from previous projects that may be relevant:\n{lessons_text}"
        )
    messages: list[BaseMessage] = [
        SystemMessage(content=_CODER_SYSTEM_PROMPT),
        HumanMessage(content=task_prompt),
    ]
    transcript: list[str] = []
    written_files: list[str] = []
    total_cost = 0.0

    for _ in range(max_steps):
        ai_message_chunk: AIMessageChunk | None = None
        async for chunk in llm_with_tools.astream(messages):
            ai_message_chunk = chunk if ai_message_chunk is None else ai_message_chunk + chunk
        ai_message: AIMessage = ai_message_chunk

        total_cost += router.cost_usd(CODER_ROLE, ai_message.usage_metadata)
        messages.append(ai_message)

        if not ai_message.tool_calls:
            return CoderTaskResult(
                success=True,
                transcript=transcript,
                written_files=written_files,
                cost_usd=total_cost,
            )

        for call in ai_message.tool_calls:
            tool_fn = tools_by_name.get(call["name"])
            observation = (
                await tool_fn.ainvoke(call["args"])
                if tool_fn is not None
                else f"error: unknown tool {call['name']!r}"
            )
            transcript.append(f"{call['name']}({call['args']}) -> {observation}")
            if call["name"] == "write_file":
                written_files.append(call["args"]["path"])
            messages.append(ToolMessage(content=str(observation), tool_call_id=call["id"]))

    return CoderTaskResult(
        success=False, transcript=transcript, written_files=written_files, cost_usd=total_cost
    )
