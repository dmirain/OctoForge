# hermes-agent

A "self-improving" single-user CLI agent in Python (Nous Research). A monolith: a god-object `AIAgent`, several
multi-thousand-line modules, a synchronous threaded streaming loop, global state, `Dict[str, Any]` at every
boundary, configuration read from deep in the stack. Storage is files (markdown memory, JSON cron) plus one
SQLite database with self-healing schema code. It lives on the operator's machine: shell, files, browser,
approval prompts in the terminal. Source read July 2026.

The interesting part is not the architecture — it is the *mechanics* around memory and learning, which are
further along than ours in specific, portable ways.

## Loop and context

**hermes-agent.** A synchronous `while` loop with an iteration limit and a refundable budget. Token accounting is
real: usage is measured against the model's window with headroom, and compression happens **inside a run** —
old iterations of a long tool-heavy run are summarized, cutting only at iteration boundaries, keeping tool pairs
atomic and protecting the last user message. There is a pre-compression hook that harvests durable facts before
history is squeezed, and a cooldown after a failed compaction. Tool results get hygiene: deduplication and
placeholders.

**OctoForge.** A typed async event stream instead of a threaded monolith; the narrative persists only user
messages, run finals and notices, so no transcript-repair machinery is needed. Token accounting exists here too
(usage captured per assistant message, `OF_MODEL_CONTEXT_TOKENS` as a trigger with a buffer, characters as a
fallback), and a provider overflow triggers synchronous compaction plus one retry. Compaction runs off the hot
path — its failure is a warning, not a dialog error — and its state is derived from the summaries rather than
kept in memory.

**Their advantage, honestly:** **mid-run compaction.** A long tool-heavy run's branch grows unbounded here;
theirs summarizes older iterations while the run continues. That is the single most portable idea in this
project, and it maps cleanly onto a `BranchCompactor` port. Also theirs: pre-compaction fact harvesting, tool
output hygiene, and a structured summary template with a length budget.

## Provider layer

**hermes-agent.** A resilient client with jittered backoff over 429/5xx/transport errors, and an auxiliary
model used for cheaper internal work.

**OctoForge.** Same retry idea, implemented as a decorator over the `LLMClient` port with a typed error taxonomy
and `Retry-After` as a floor. Prompt caching is respected structurally (byte-stable system prompt, date envelope
at the tail).

**Worth taking:** their auxiliary-model split. Our router runs on the main model and is called for almost every
message; a second client instance for router and compactor is pure composition-root work, no port change.

## Memory and learning — where they are ahead

**hermes-agent.** Memory is markdown files plus FTS5 (BM25) search with snippets and windows **across all
sessions**. Memory is injected into context automatically rather than waiting for the agent to search. There is
a learning loop: a post-run review fork, a curator, nudges; budgets and consolidation of memory; and threat
scanning of stored records.

**OctoForge.** Memories are rows in the instruction store (private, never publishable), ranked by the same
embeddings machinery as skills and knowledge, and reached through `recall`. Storage is one migratable database
rather than scattered files, and multi-user ownership is a SQL predicate.

**Their advantage, honestly:**

- **Automatic memory injection.** Ours depends on the agent deciding to search — the system prompt and a system
  skill push it to, but a nudge is weaker than a guarantee.
- **A learning loop.** We store what the agent chooses to save and count usage; nothing reviews, promotes,
  consolidates or retires records.
- **Cross-dialog search.** `history_search` is scoped to one dialog; they search every session.

**Worth taking, in order:** a budgeted memory block assembled next to the topics block at branch build time;
pre-compaction harvesting of durable facts; recall tracking on records; then, much later, a curator.

**Deliberately not taking:** file-backed memory with watchers, a memory wiki, "dreaming" narratives.

## Skills

**hermes-agent.** Skills are shipped scripts and markdown, addressed through a mandatory index in the prompt
plus a `skill_view` call for the full text — two-level progressive disclosure. Execution of a skill can be
running its script.

**OctoForge.** Skills are rows found by embedding search with an optional cross-encoder rerank; `recall` returns
**whole records**, not snippets, because a truncated scenario cannot be followed. Skills are text, never
executable code, and the agent's actions go through declared HTTP contracts under the SSRF guard.

**Their advantage:** the mandatory index means the agent always knows what exists; ours must retrieve well. In
exchange, ours scales past what fits in a prompt and isolates per user.

**Worth considering:** a compact index (type + title + tags) in the system prompt, with full content on demand.
It trades prompt-cache stability for guaranteed awareness — measure before adopting.

## Storage and configuration

**hermes-agent.** Files plus one SQLite database with schema self-healing, three different claim mechanisms in
cron, locks and heartbeat hacks around threads, and configuration read from within the call stack.

**OctoForge.** One schema with Alembic, ports with dependency injection from one composition root, UTC enforced
by the column type, and a cron claim that is one SQL compare-and-swap.

## Where each is stronger

**hermes-agent is stronger at:** memory mechanics (automatic injection, cross-session search, consolidation),
the self-improvement loop, mid-run context compression, and host-level capability through shell and scripts.

**OctoForge is stronger at:** architectural discipline that keeps the surface small (ports, DTOs, one store,
strict typing), multi-user isolation, semantic routing between concurrent obligations, structured datasets with
validation, and the security posture that follows from having no exec.

**The honest summary:** hermes-agent shows what a memory-centric agent looks like when someone pushes that idea
hard, and pays for it with a monolith whose self-healing code is its own tax. The memory and mid-run compaction
ideas are worth porting; the architecture is a cautionary example.
