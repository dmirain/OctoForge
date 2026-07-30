# opencode

An open-source AI coding agent: TypeScript monorepo, CLI and TUI, living on a developer's machine. Its strength
is code editing — file tools, git-tree snapshots, a permission system — plus a hosted commercial side (an LLM
gateway with workspace tenants, billing and quotas). Architecturally it is Effect-ts: services as
`Context.Service` + `Layer`, typed errors, structured concurrency on fibers, and an **event-sourced** session
core (an `event` table with projections, a durable inbox, replay from a cursor). Source read July 2026 (branch
`dev`).

Different product, overlapping engine — which makes it the most useful comparison for the run loop and the
context layer specifically.

## The run loop

**opencode.** An outer loop over pending work, an inner loop over steps. A step is: choose context → promote
pending inputs → auto-compact if needed → request → stream. LLM events become **durable session events**
published through a semaphore; tool calls run as concurrent fibers in a fiber set. Retries are classified
(rate-limit, provider-internal, transport), with exponential backoff floored by `Retry-After`, ≤4 attempts and a
`RetryScheduled` event. At the last step, tools are removed from the request and `toolChoice: "none"` plus a
synthetic message **forces a final answer** instead of erroring out. Cancellation is `Fiber.interrupt` with
structural cleanup — and the partial text is lost. Messages arriving mid-run go through a deterministic
two-lane durable inbox (steer / follow-up).

**OctoForge.** Concurrent tool execution as well, started eagerly as arguments finish streaming rather than at
step end. The same retry classification and `RetryScheduled` event exist here (transient kinds only, and a
stream is retried only before its first event). Cancellation keeps the partial text and fills a reply for every
tool call, so the transcript stays provider-valid. Instead of a durable inbox with lanes, an arriving message is
persisted to the narrative and every live process re-reads it at its next iteration (the pull model).

**Different by design:** they have one linear session where a topic change is an interruption or a new session;
here several obligations run at once, and semantic routing decides which one a message belongs to. Their
background work surfaces as a synthetic nudge; ours folds into the dialog through the outbox with delivery
tracking.

**Worth taking:** the forced final at the iteration cap (an extra call with no tools and "summarize what you
have" beats our `Failed`); tool-progress events for long calls.

**Deliberately not taking:** event sourcing with projections. It buys replay we do not need — our narrative is
already the durable story, and terminals plus task rows cover recovery — at the cost of a projector layer.

## Context

**opencode.** Auto-compaction inside the step, driven by real usage against the model's limit with headroom. The
"recent" part is a flattened serialization with tool outputs truncated to 2000 characters. A failed
auto-compaction fails the user's step. Pre-checkpoint rows are effectively dead to the model.

**OctoForge.** Compaction is background work: a failure is a logged warning, never a dialog error, and the hot
tail is *real messages with roles* rather than a flattened blob. The token trigger exists here too
(`OF_MODEL_CONTEXT_TOKENS` minus `OF_CONTEXT_BUFFER_TOKENS`), with the character threshold as a fallback, and a
provider overflow triggers synchronous compaction plus one retry. Summaries carry topic tags and stay reachable
through `history_search`, so the archive is not dead to the model.

**Worth taking:** their more structured summary template; a manual compaction command.

## Provider layer

**opencode.** Provider registry with lazy adapters over official SDKs, three layers of retry, real model
failover along a chain, usage accounting and cost tracking, plus billing and quotas on the hosted side.

**OctoForge.** One OpenAI-compatible client with a typed error taxonomy
(`rate_limit`/`auth`/`quota`/`context_overflow`/`provider_internal`/`transport`/`client`), retries for the
transient kinds, usage capture per assistant message, and an idle-stream watchdog. Embeddings and reranking are
first-class ports with local and HTTP backends — opencode has neither.

**Worth taking:** model failover chains; usage aggregation into something an operator can read (we store
per-message counts and stop there — see [../limitations.md](../limitations.md)).

## Tools and permissions

**opencode.** A permission system built for a machine with files: allow/deny/ask per action, argument validation
against schemas, and hooks. MCP is supported, which opens the whole tool-server ecosystem.

**OctoForge.** No exec and no file tools, so the permission question is narrower: tools are registered
explicitly, visibility is per-invocation (`visible_to`), and identity comes from the context rather than from
arguments. There is no permission policy, no central argument validation and no MCP — all three are named in
[../limitations.md](../limitations.md).

**Worth taking:** central argument validation against `spec.parameters_schema` in the loop (uniform errors, the
schema becomes the single source of truth); a `PermissionPolicy` port with allow/deny (without `ask`, which
needs a human in the loop we do not have); a hooks port whose first consumer is an audit log.

**Deliberately not taking:** file/exec tools and the machinery they require.

## Storage

**opencode.** Blocking sync driver behind a semaphore (fine for a CLI), hand-written DDL, epoch-millis
timestamps, a suspend→consume→resume CAS chain for durable inbox recovery, and a set of SQLite pragmas (WAL,
busy_timeout, synchronous, foreign_keys) that a local single-file database wants.

**OctoForge.** Async all the way through, Alembic migrations, and UTC enforced by the column type rather than by
convention. Restart recovery comes from task rows and exchange statuses instead of an event log. Deletions
cascade explicitly in the stores rather than relying on database-level cascades, which is what keeps SQLite
correct without pragmas — though a file-backed SQLite deployment would still benefit from WAL and a
`busy_timeout`.

**Worth taking:** the pragma block for file-backed SQLite.

## Where each is stronger

**opencode is stronger at:** coding work (file tools, snapshots, rollback), provider abstraction and failover,
permissions and MCP, cost accounting, and the recovery guarantees that event sourcing gives.

**OctoForge is stronger at:** conversational semantics (several questions in flight, semantic routing, obligations
as rows), background and scheduled work folded into the dialog, memory and knowledge as searchable owned data,
messenger surfaces, multi-user isolation, and simplicity for the same guarantees — an asyncio actor against a
runner/publisher/projector/pending stack.

**The honest summary:** opencode is a coding agent whose session engine is worth studying; OctoForge is a
conversational platform whose engine solves a different concurrency problem. The overlap is the loop, and there
the useful borrowings are small and specific.
