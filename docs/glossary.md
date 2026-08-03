# Glossary

Words used precisely in this documentation and in the code. Where a term has a common industry
meaning that differs, the difference is stated.

### Actor

The per-dialog object that owns everything about one conversation: its narrative, its exchanges, its
running processes, and delivery of results. Implemented as `ConversationRunner` in
`agent/runner.py`, with a command inbox serialized by one asyncio task. One actor per dialog; a
`ConversationManager` maps `(user_id, channel)` to it.

### Answer run

A process whose job is to satisfy one exchange — the internal mechanics of replying to a user. Backed
by a task row of kind `ANSWER`. Invisible to the agent's own tools: `task_list` does not show them.

### Attachment

A non-text part of an incoming message (image, voice recording), carried on `ChatMessage.attachments`
as an `Attachment` with an `AttachmentKind`. Transports resolve the bytes; the core sees the
description or transcript.

### Branch

The message list one process sends to the model on one iteration: the system prompt, a snapshot of
the narrative with role marks applied, and the process's own private working suffix (tool calls and
results of this run). Branches are rebuilt from the narrative at every iteration — the *pull model* —
so a message that arrives mid-run is seen by every live process exactly once. Marks exist only in the
branch copy; the stored narrative keeps clean text. See `agent/branch.py`.

### Capability report

The block logged at startup listing every optional capability as on or off with the endpoint or model
behind it. Produced by `server/capabilities.py`. Never contains secret values.

### Channel

A string identifying the surface a dialog happens on: `"web"`, `"telegram"`, or whatever an embedder
chooses. Half of a dialog's identity, so the same person's web and Telegram conversations are
separate dialogs.

### Collection

A `COLLECTING` exchange accumulating forwarded material that has not been reacted to yet. One per
dialog at a time. Either adopted by a question or promoted by the sweep once quiet.

### Compaction

Replacing the older part of a narrative with a summary while keeping a verbatim hot tail, so the
prompt stays bounded as a dialog grows. Triggered by size thresholds or reactively on a provider's
context-overflow error. See [reference/context-compaction.md](reference/context-compaction.md).

### Dialog

The durable conversation identified by `(user_id, channel)`. A row in `dialogs`, plus its messages,
exchanges, summaries and tasks.

### Endpoint record

An instruction record of type `endpoint` describing one callable HTTP endpoint: method, URL template,
parameter schema, and optionally which stored secret authenticates it. `external_call` executes it;
`endpoint_get` fetches its contract first (*late binding*). Not to be confused with an HTTP route of
this application's own API.

### MCP mirror

A public endpoint record (`mcp/{server}/{tool}`, `kind: "mcp"` in its content) representing one tool
of a registered external MCP server. Written only by the MCP sync; executed by `external_call`
through the MCP delegate. The mirror exists because the MCP protocol has no search — it is a
persistent, embedded cache of `tools/list`. See [reference/mcp.md](reference/mcp.md).

### Exchange

One durable obligation to a user: their question, its clarifications, and the answer that settles it.
A row in `exchanges` with a status
(`OPEN`, `IN_PROGRESS`, `ANSWERED`, `AWAITING_USER`, `CANCELLED`, `FAILED`, `COLLECTING`); messages
reference it through `messages.exchange_id`. **An exchange is not a task** — see *Task*.

### Hot tail

The most recent messages of a narrative kept verbatim after compaction, as opposed to the summarized
part.

### Instruction

The generic name for a record in the `instructions` table. Its `type` decides what it is: `skill`
(how to do something), `knowledge` (a fact), `endpoint` (a callable contract), `memory` (something
about one user). Ranked and returned by `recall`. Records are private to an owner or public to the
installation; some are `system`-owned and synced from a registry in code.

### Loop (agent loop)

`AgentLoop.stream()`: the LLM ↔ tools iteration, exposed as an async iterator of typed events. One
iteration is one model call plus the tool round it triggers. Knows nothing about dialogs, users or
exchanges.

### Material

Content a user shares rather than asks about — a forward, an image without a caption. Carried as
`MessageKind.MATERIAL`, accumulated in a collection, and never treated as an instruction addressed to
the agent.

### Narrative

The dialog's durable, append-only story: user messages, run finals, broker notices. Only these are
persisted; streaming deltas are not. The narrative is what branches are rendered from, and what is
reloaded after a restart.

### Nudge

An event-driven reminder sent when an exchange has been `AWAITING_USER` for a while and the user has
not replied — the agent re-asks its question instead of waiting forever.

### Outbox

The actor's retention buffer for results that have no subscriber attached yet (`_pending_deliveries`).
It exists so a background result is not lost when nobody is listening; it is not a replay log for
live stream events. `delivered_at` on the task row records that delivery happened.

### Port

A `Protocol` describing something the core needs from the outside: `LLMClient`, `EmbeddingClient`,
`RerankerClient`, `MessageRouter`, `PromptProvider`, `InstructionStore`, `TaskStore`, `CronStore`,
`CronWaker`, `VisionClient`, `TranscriptionClient`, `SearchProvider`, and the module stores. Ports are
the seams an embedder replaces.

### Process

An in-memory run inside an actor, always backed by a task row. Either an answer run or a RUN task.
"Process" here has nothing to do with an OS process — everything happens in one asyncio event loop.

### Recall

The tool (and the operation) that searches the instruction store by meaning across skills,
knowledge, dataset descriptors and the user's memories, returning full records. Endpoint records are
deliberately excluded from the default results and only surface with `type=endpoint`. The agent's
first move for anything non-trivial.

### Run

One execution of the agent loop. A process performs one or more runs over its lifetime; a run ends
with a final answer, a cancellation or a failure.

### RUN task

A process created by `task_create` (or a cron firing) that owes no user an answer directly. Its result
is delivered whole through the outbox when it finishes.

### Skill

An instruction record of type `skill`: a scenario telling the agent how to do something. Stored, not
compiled — a skill is text plus tags, retrieved by `recall`, not a plugin.

### System record

An instruction record marked `system`, upserted at startup from a declarative registry in code
(`CORE_SYSTEM_SKILLS`, `WEB_SYSTEM_SKILLS`), optionally patched per deployment by a JSON overlay.
Agent-facing save and delete refuse to touch them.

### Task

A row in `tasks`: the durable record of a unit of work, with a kind (`ANSWER` or `RUN`), a status, and
delivery bookkeeping. Rows are kept forever, terminal states included. Tasks are how processes survive
restarts and how results are proven delivered. An exchange is a promise to a person; a task is work
the system did.

### Watermark

How far into the narrative a process has already incorporated. Used when re-syncing a branch, and
shifted when the in-memory narrative is trimmed after compaction.
