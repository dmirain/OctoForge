# Embedding the core

`octoforge-core` is a library. This is how to use it from your own application without dragging in the web
adapter — and how deep you have to go for what you want.

## Install

```bash
pip install -e core                      # from the repo root; no FastAPI involved
pip install -e "core[local-embeddings]"  # only for the in-process embedding/rerank backends
pip install -e "core[postgres]"          # asyncpg
```

Dependencies are httpx, SQLAlchemy, Alembic, numpy, croniter, cryptography. The package ships `py.typed`.
Importing it never requires torch — only constructing `SentenceTransformerEmbedder` or
`CrossEncoderReranker` does, and each raises an `ImportError` naming the install command if the extra is
missing.

## Three depths

| Depth | You get | You provide |
|---|---|---|
| `AgentLoop` | The LLM ↔ tools event loop | An `LLMClient` and a `ToolRegistry`. No database, no dialogs |
| `ConversationManager` | Dialogs: persistence, exchanges, concurrent processes, routing, subscriptions | The above plus stores and a prompt provider |
| Full platform | Instructions, datasets, memories, secrets, cron, tasks, the whole toolbox | The composition of all of it — follow `runtime()` |

### Depth 1: just the loop

```python
loop = AgentLoop(llm, registry, AgentLoopConfig(max_iterations=10))
async for event in loop.stream(
    messages, LoopControl(), ToolContext(user_id="u", channel="cli", dialog_id="d")
):
    ...
```

Useful when you already own conversation state and want the tool-calling cycle with typed events, eager tool
execution and safe cancellation. See [../reference/agent-loop.md](../reference/agent-loop.md).

### Depth 2: dialogs

The [README](../../README.md) carries a complete ~50-line example: engine, SQLite, `ConversationManager`,
subscribe, submit, print deltas. Two details it depends on:

- **subscribe before submit**, or the first events are gone;
- events arrive as `ConversationEvent(dialog_id, seq, exchange_id, payload)` — the payload is the loop event.

### Depth 3: the platform

Use the facade in `core/src/octoforge_core/composition.py` and follow
`deploy/src/octoforge_deploy/main.py:runtime()` as the reference wiring. The builders take ports and
dataclass configs only, never a web settings object, so your composition root reuses them instead of
copying:

```python
registry = build_tool_registry(
    ToolDependencies(
        outbound_http=outbound_http,
        guard=guard,
        stores=ToolStores(
            tasks=task_store, cron=cron_store, archive=summaries, summaries=summaries
        ),
        services=ToolServices(
            instructions=instructions, datasets=datasets, executor=executor
        ),
    ),
    _your_limits,
)
manager = build_conversation_manager(
    config=build_runner_config(
        RunnerServices(
            loop=build_agent_loop(llm, registry, AgentLoopConfig(max_iterations=10)),
            prompts=prompts,
            router=build_router(llm, prompts, timeout_seconds=10.0),
            compactor=build_compactor(
                CompactorServices(summaries, summaries, llm), CompactorConfig(...)
            ),
        ),
        RunnerOptions(max_processes=5),
    ),
    stores=ManagerStores(...),
    ownership=OwnershipConfig(node_id="my-node"),
)
```

`deploy/tests/test_modularity.py` is a working third-party composition root doing exactly this — file-backed
prompts, a fake search provider, an in-memory instruction store — and running a dialog end to end. Read it
as the executable version of this guide.

## What you will want to replace

| Concern | Port | Notes |
|---|---|---|
| Model provider, routing between models, failover | `LLMClient` | The shipped client is OpenAI-compatible with retries; wrap or replace it |
| Prompts | `PromptProvider` | Or point `OF_*_PROMPT_SOURCE` at files if you use the web settings |
| Whose message is this | `MessageRouter` | A static policy is a class with one method |
| Ranking and storage of knowledge | `InstructionStore` (+ `InstructionVectorSearch`), `EmbeddingClient`, `RerankerClient` | pgvector or an external vector DB fits here |
| History policy | `ContextCompactor` | `NoopContextCompactor` if you manage history yourself |
| Scheduling | `CronStore` / `CronWaker` / `Scheduler` | Or drive the firing contract from an external scheduler |
| Delivery surface | none — subscribe to the event stream | See [add-a-surface.md](add-a-surface.md) |

## Schema management

`init_db(engine)` (`create_all`) is for tests and quick experiments. In production use
`bootstrap_schema(engine)`, which runs Alembic to head and, on an empty non-SQLite database, creates from the
models and stamps head. Your application inherits the migration chain in
`core/src/octoforge_core/db/migrations/`.

If you add tables of your own, keep them in your own metadata and your own chain — the core's chain is
append-only and shared by every installation.

## Things the core will not do for you

- **No web framework, no auth.** Identity arrives as strings (`user_id`, `channel`); deciding who the user is
  belongs to your application.
- **No transport.** Nothing downloads a picture or sends a message; that is what `ImageResolver` and your
  renderer are for.
- **No background loop management.** `CronScheduler` and `CollectingSweeper` are objects you start and stop.
- **No settings.** `Settings` lives in the web package; the core takes explicit configs.

## Code anchors

- `core/src/octoforge_core/composition.py` — the stable builder facade
- `core/src/octoforge_core/composition_agent.py`,
  `core/src/octoforge_core/composition_runtime.py` — agent and runtime builders
- `deploy/src/octoforge_deploy/main.py`, `deploy/src/octoforge_deploy/runtime_entry.py` — `runtime()`
  and the reference composition root
- `deploy/tests/test_modularity.py` — a third-party composition root, executable
- `core/src/octoforge_core/__init__.py` — the public surface of the package
- [../architecture.md](../architecture.md) — the port table and the dependency rule
