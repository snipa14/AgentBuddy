"""Generic LangGraph node for a plugin-declared agent role (2026-08-06 design).

Every plugin role executes through this one node, parameterized by its
manifest -- structurally identical to node_reviewer/node_static_checks's
verdict-on-a-diff shape, just with the skill text and gating/advisory
behavior supplied by the plugin instead of hardcoded.
"""

import asyncio
import logging
from typing import Any, Literal

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from services.orchestrator.agents.llm import invoke_structured
from services.orchestrator.executor_client import ExecutorClient
from services.orchestrator.graph.nodes import _reopen_last_done_task_or_block, _with_cost
from services.orchestrator.graph.state import ProjectState
from services.orchestrator.model_router.router import ModelRouter
from services.orchestrator.plugins.manifest import PluginManifest

logger = logging.getLogger(__name__)


class PluginFinding(BaseModel):
    """A single plugin-review finding with file, line, severity, and message."""

    file: str = Field(description="File path where the issue was found")
    line: int = Field(description="Approximate line number of the issue")
    severity: str = Field(description="'critical' or 'warning'")
    message: str = Field(description="Concise description of the issue")


class PluginVerdict(BaseModel):
    """A plugin role's verdict and structured findings."""

    verdict: Literal["approve", "changes-requested"] = Field(
        description="'approve' if no issues; 'changes-requested' if at least one finding"
    )
    findings: list[PluginFinding] = Field(default_factory=list)


def _build_user_prompt(project_id: str, files: dict[str, str]) -> str:
    parts = [f"Project: {project_id}\n\n"]
    for path, content in sorted(files.items()):
        parts.append(f"### {path}\n```\n{content}\n```\n")
    return "\n".join(parts)


async def run_plugin_review(
    router: ModelRouter,
    executor_client: ExecutorClient,
    project_id: str,
    role: str,
    skill_body: str,
    changed_files: list[str],
) -> tuple[PluginVerdict, AIMessage]:
    """Runs one plugin role's review pass over the project's changed files.

    Args:
        router: The ModelRouter used to resolve `role`'s provider.
        executor_client: The Executor client used to read file contents.
        project_id: The project identifier.
        role: The plugin's model_router role name.
        skill_body: The plugin's SKILL.md body, sent as the system prompt.
        changed_files: File paths modified during coding. If empty, every
            non-directory entry in the project root is read instead (mirrors
            review_changes()'s fallback in agents/reviewer.py).

    Returns:
        A tuple of (PluginVerdict, raw AIMessage) -- the raw message carries
        usage_metadata for the calling node to price via ModelRouter.cost_usd().
    """
    llm = router.resolve(role)
    files: dict[str, str] = {}
    if changed_files:
        for path in changed_files:
            try:
                files[path] = await executor_client.read_file(project_id, path)
            except Exception as exc:
                logger.warning(
                    "project %s: plugin role '%s' failed to read '%s': %s",
                    project_id, role, path, exc,
                )
    else:  # no changed_files list — list all and read each one
        try:
            entries = await executor_client.list_files(project_id, ".")
            for entry in entries:
                if not entry.is_dir:
                    try:
                        files[entry.name] = await executor_client.read_file(project_id, entry.name)
                    except Exception as exc:
                        logger.warning(
                            "project %s: plugin role '%s' failed to read '%s': %s",
                            project_id, role, entry.name, exc,
                        )
        except Exception as exc:
            logger.warning(
                "project %s: plugin role '%s' failed to list files: %s", project_id, role, exc
            )

    messages = [
        SystemMessage(content=skill_body),
        HumanMessage(content=_build_user_prompt(project_id, files)),
    ]
    return await invoke_structured(llm, PluginVerdict, messages)


def node_plugin(
    state: ProjectState,
    router: ModelRouter,
    executor_client: ExecutorClient,
    manifest: PluginManifest,
    skill_body: str,
    next_node: str,
) -> dict[str, Any]:
    """Runs one plugin role's review pass and updates state accordingly.

    A 'gating' plugin's changes-requested verdict reopens the most recently
    completed task for another Coder attempt (MAX_TASK_ATTEMPTS-bounded,
    same contract node_static_checks uses), and clears any `pending_approval`
    already armed on state -- the only slot where this can be non-None going
    in is `after_tester`, since `node_testing`'s success branch arms a
    merge_approval there; leaving it set would desync the merge gate once
    control routes back to `coder` instead of offering that approval. Every
    other outcome -- 'gating' approve, or any 'advisory' verdict -- is a
    cost-only update; route_after_plugin sends control to `next_node`.

    Args:
        state: The current project state.
        router: The ModelRouter this plugin's role resolves through.
        executor_client: The Executor client used to read changed files.
        manifest: This plugin's validated manifest.
        skill_body: This plugin's SKILL.md body.
        next_node: This slot's next node -- the next plugin in the chain,
            or the slot's real exit node if this is the last one (baked in
            at graph-build time, graph/build.py).

    Returns:
        A state update: task_queue/phase/messages on a gating
        changes-requested outcome (or a blocked transition at
        MAX_TASK_ATTEMPTS); a cost-only update otherwise.
    """
    result, ai_message = asyncio.run(
        run_plugin_review(
            router, executor_client, state["project_id"], manifest.role, skill_body,
            state["changed_files"],
        )
    )
    cost = router.cost_usd(manifest.role, ai_message.usage_metadata)

    if manifest.mode == "gating" and result.verdict == "changes-requested":
        findings_text = "\n".join(
            f"- {f.file}:{f.line} [{f.severity}] {f.message}" for f in result.findings
        )
        feedback = AIMessage(content=f"{manifest.name} feedback:\n{findings_text}")
        update = _reopen_last_done_task_or_block(state, feedback, manifest.name)
        update["pending_approval"] = None
        return _with_cost(state, update, cost)

    if result.verdict == "changes-requested":
        logger.info(
            "project %s: advisory plugin '%s' flagged %d finding(s), continuing",
            state["project_id"], manifest.name, len(result.findings),
        )

    return _with_cost(state, {}, cost)


def route_after_plugin(state: ProjectState, next_node: str) -> str:
    """Routes to Coder on a gating changes-requested, the Memory Curator on
    blocked, or onward to `next_node` otherwise."""
    if state["phase"] == "blocked":
        return "memory_curator"
    if state["phase"] == "implementing":
        return "coder"
    return next_node
