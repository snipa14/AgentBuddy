# Phase 18 — Multi-Project Concurrency: Design

**Status:** approved, not yet implemented
**TODO.md phase:** 18
**Decisions depended on:** ADR-006 (global orchestrator + per-project LangGraph thread)

## Goal

More than one project can be active at once without state bleed, with sane
sharing of the GPU (local inference) and the global cost budget. Per
TODO.md, this phase does not touch anything already built beyond what's
needed to make these three properties true and tested.

## Context

- `OrchestratorService` compiles **one** project subgraph
  (`build_project_graph`) and dispatches every request onto it keyed by
  `thread_id == project_id` (`service.py`). The graph, `ModelRouter`, and
  `ExecutorClient` instances are shared across every project; only
  `ProjectState` is per-thread.
- FastAPI's endpoint functions in `api.py` are plain `def`s, which FastAPI
  runs in its threadpool — two webhook/resume calls for two different
  projects can already run concurrently today, in separate OS threads, each
  calling `graph.invoke()` synchronously.
- Every LLM-backed node is a synchronous `def` that bridges to the async
  agent layer via a single `asyncio.run(...)` call (ADR-019). Each such call
  gets its own event loop, scoped to whichever thread is executing that node.
- `node_cost_check` (`graph/nodes.py`) currently checks the *current
  project's* `cost_usd_session` against a hardcoded `COST_THRESHOLD_USD_STUB
  = 50.0` — a per-project, per-session check, not the global weekly budget
  ADR-009 actually specifies.
- `services/orchestrator/graph/checkpointer.py` builds a `SqliteSaver` over
  one `sqlite3.Connection` (`check_same_thread=False`) shared across all
  threads. `langgraph.checkpoint.sqlite.SqliteSaver` keeps its own internal
  `threading.Lock` around checkpoint reads/writes for exactly this reason —
  concurrent `graph.invoke()` calls from separate OS threads are already
  safe at the persistence layer. This phase doesn't change that; it's called
  out here so the design doesn't accidentally re-solve it.

## Design

### 1. Global cost aggregation replaces the per-project stub gate

New module `services/orchestrator/graph/cost_aggregator.py`:

```python
def other_projects_cost_usd_weekly(
    checkpointer: BaseCheckpointSaver, exclude_project_id: str
) -> float:
    ...
```

`checkpointer.list(None)` returns every historical checkpoint *version* for
every thread, not just the latest one per thread (`service.list_projects()`
already has to de-dupe thread_ids into a set for this reason). Summing
`cost_usd_weekly` across every row would count one project's cost dozens of
times over. The aggregator instead:

1. Collects distinct thread_ids via `checkpointer.list(None)`, excluding
   `exclude_project_id`.
2. For each remaining thread_id, fetches only its *latest* checkpoint via
   `checkpointer.get_tuple({"configurable": {"thread_id": tid}})`.
3. Sums `checkpoint["channel_values"].get("cost_usd_weekly", 0.0)` across
   those latest checkpoints.

Counts every known thread regardless of `phase` — a finished project's
spend still counts against a shared weekly budget, so terminal (`done`/
`blocked`) threads are included, not filtered out.

`node_cost_check` changes to:

```python
def node_cost_check(
    state: ProjectState, checkpointer: BaseCheckpointSaver, threshold_usd: float
) -> dict[str, Any]:
    global_total = state["cost_usd_weekly"] + other_projects_cost_usd_weekly(
        checkpointer, state["project_id"]
    )
    if global_total < threshold_usd:
        return {}
    # ...trip the gate; pending_approval summary reports global_total vs threshold_usd
```

Using `state["cost_usd_weekly"]` for the *current* project (rather than
re-fetching its own checkpoint) keeps that half as fresh as the in-flight
superstep; only every *other* project is read from the checkpointer. This
fully replaces today's `cost_usd_session`/`COST_THRESHOLD_USD_STUB` check —
there is one cost gate, and it is global.

`checkpointer` and `threshold_usd` are threaded into `node_cost_check` via
`functools.partial` in `build.py`, the same pattern every other node
dependency (`router`, `executor_client`, `projects_root`) already uses.

### 2. Configurable global threshold

`OrchestratorConfig` (`config.py`) gets a new field:

```python
global_cost_threshold_usd: float
```

Read from a `GLOBAL_COST_THRESHOLD_USD` env var in `from_env()`, defaulting
to `50.0` (matching today's stub value, so existing deployments don't see a
silent behavior change). Threaded through `main.py`/`registry/cli.py`'s
existing config-loading path into `build_project_graph()` as a new
parameter, alongside `checkpointer`.

### 3. GPU serialization for local inference

A plain `asyncio.Semaphore` cannot coordinate across the separate event
loops each `asyncio.run()` call creates in each project's OS thread, so this
needs a `threading.Semaphore(1)`.

Rather than touching every agent module (`planner.py`, `coder.py`,
`reviewer.py`, `frontend_reviewer.py`, `memory_curator.py` — all
phase-complete and independently tested), the throttle is scoped entirely
inside `ModelRouter`. A repo-wide grep confirms the only methods ever called
on a `router.resolve()`-returned model are `.bind_tools()`,
`.with_structured_output()`, and then `.ainvoke()`/`.astream()` on whatever
those return (`agents/llm.py::invoke_structured`, `agents/coder.py`'s
tool-calling loop). A small duck-typed wrapper covers exactly those four
entry points:

```python
class _ThrottledChatModel:
    def __init__(self, model: BaseChatModel, semaphore: threading.Semaphore) -> None: ...
    async def ainvoke(self, *args, **kwargs): ...       # acquire, await, release
    async def astream(self, *args, **kwargs): ...       # acquire for the whole generator's lifetime
    def bind_tools(self, *args, **kwargs) -> "_ThrottledChatModel": ...           # re-wrap result
    def with_structured_output(self, *args, **kwargs) -> "_ThrottledChatModel": ...  # re-wrap result
```

`ModelRouter.__init__` wraps only `ChatOllama` instances in
`_ThrottledChatModel` before storing them in `self._models`; `ChatAnthropic`
entries are stored unwrapped, so Claude-API-bound work runs in parallel
without this constraint. One `threading.Semaphore(1)` is shared across every
Ollama-backed provider entry in a given router instance (one physical GPU,
regardless of how many named Ollama tiers `models.yaml` declares).
`resolve()`'s signature, return-type usage, and every existing call site are
unchanged.

### 4. Proving thread isolation (no state bleed)

New test in `graph/tests/unit/test_concurrency.py`: build one compiled
graph, drive fixture project A through a few nodes to a paused interrupt,
then drive fixture project B (deliberately different `task_queue`/`plan`
content) through its own steps, then resume A — assert A's final state
contains none of B's `task_queue`/`plan` values and vice versa. This only
needs sequential `graph.invoke()` calls against different `thread_id`s
(LangGraph's own per-thread state isolation is what's under test, not true
OS-thread concurrency), matching TODO.md's literal "interleave two fixture
projects" wording.

### 5. Tests

- `graph/tests/unit/test_cost_aggregator.py`: a fake checkpointer seeded
  with multiple projects' checkpoints, including *multiple historical
  versions* for at least one project, specifically to catch the
  double-counting bug described in §1. Asserts the sum uses only the latest
  checkpoint per thread and excludes the current project.
- `graph/tests/unit/test_nodes.py` additions: `node_cost_check` trips the
  gate when the sum of *other* projects' cost plus its own crosses
  `threshold_usd`, even though its own `cost_usd_weekly` alone is below it;
  does not trip when the sum is still under threshold.
- `model_router/tests/unit/test_router.py` additions: two "simultaneous"
  scripted calls dispatched through a `ThreadPoolExecutor` against a
  slow/instrumented `ChatOllama`-shaped stand-in never overlap (assert via a
  shared counter that max-concurrent == 1 across the two calls); the same
  test shape against an unthrottled Anthropic-backed pair *does* overlap,
  proving Claude calls aren't serialized.
- §4's thread-isolation test.

## Definition of Done mapping

- "the interleaved two-project test shows no state bleed" → §4.
- "the serialization test proves only one local-inference call is ever in
  flight" → §5's router test.
- Cost aggregation correctness → §5's cost_aggregator + node_cost_check
  tests.

## Verification expectations

Consistent with every phase since Phase 5: `graph/` tests need
`langgraph`/`langchain-core` (not installed in this sandbox as of this
writing); `test_cost_aggregator.py` has no such dependency (pure Python +
a fake checkpointer object) and should be verifiable for real regardless.
`test_router.py`'s throttle test needs `langchain-ollama`/
`langchain-anthropic`. Document what actually ran vs. what was hand-traced
in TODO.md's verification-status note, per this repo's established
practice — do not claim a phase done based on hand-tracing alone.

## Out of scope for this phase

- Any reset/rollover logic for `cost_usd_weekly` (it accumulates forever
  today, per-project, and this phase doesn't change that — only how it's
  aggregated across projects).
- A configurable concurrency limit greater than 1 for local inference (GPU
  is a single physical resource; revisit only if a real multi-GPU setup
  exists).
- Explicit locking in `OrchestratorService` around `graph.invoke()` calls —
  `SqliteSaver`'s own internal lock already covers checkpoint-write safety
  under concurrent OS threads (see Context).
