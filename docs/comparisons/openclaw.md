# openclaw

A personal AI assistant in TypeScript: one gateway process holding ~20 messaging channels (Telegram, WhatsApp,
Signal, iMessage, Slack…), conversations, cron, skills, shell and file tools, voice and a canvas. Multi-user
means one container per person. Source read July 2026.

It is the closest thing to OctoForge in *ambition* and the furthest in *architecture*: a very rich single-owner
assistant built on global singletons and file-backed stores, against a smaller multi-user system built on ports
and SQL.

## The run loop

**openclaw.** Not a plain reason-act loop but an orchestrator of failure handling: auth-profile rotation, model
fallback chains, retry of empty responses, an idle-timeout interrupter, and a guard against runaway loops after
compaction. One active run per session; a message arriving during a run is *steered* into it with typed refusal
reasons. Two queues (steer vs follow-up) with debounce and burst coalescing into a single turn, plus an
overflow policy. Errors are data: an assistant message with `stopReason: "error"` keeps the history valid. On
gateway restart it injects a "previous turn was interrupted" note and resumes. Inbound turns carry an
idempotency key. Hooks (`before_tool_call`, `after_tool_call`) can block, rewrite or require approval.

**OctoForge.** `AgentLoop.stream()` is an async generator of typed events with eager, concurrent tool
execution; the actor owns processes and delivery. Transient provider failures are retried below the loop
(classified, exponential backoff with jitter, `Retry-After` as a floor); a stream is only retried before its
first event. Errors are data here too — a failing tool returns `error: …` to the model, and an interrupted turn
keeps its partial text with a reply for every tool call. Submissions carry `client_message_id` as an
idempotency key.

**Different by design:** several answers stream at once, each owning its exchange, instead of one active run
with steering. That removes the whole steer/follow-up lane machinery — there is nothing to steer *into*
because a second question simply gets its own run.

**Worth taking:** debounce and coalescing of a burst of messages into one turn (we call the router once per
message); a per-process idle watchdog (we watch the LLM stream, not the process); model fallback chains; a
system note in the narrative when a run fails, so compaction and routing see the whole story.

**Deliberately not taking:** steering lease/ack protocols, lane priorities, hook-based approval — single-tenant
machinery, and the approval half is moot without exec tools.

## Context and prompt caching

**openclaw.** Strict prefix-cache discipline: `stablePrefix + dynamicSuffix` with an explicit boundary marker;
timezone in the prompt, current time in an envelope. Transcripts are JSONL files with a substantial repair and
caching layer, migrating to SQLite. Compaction is elaborate, with its own state pointer.

**OctoForge.** The same prompt discipline, reached independently: the system prompt is byte-stable, and the
current date and time ride as an envelope on the last branch message. Storage is one transactional schema, so
there is no repair layer; the compaction boundary is *derived* from the summaries (`max(seq_to)`) rather than
stored as a pointer, so it cannot drift. Provider-reported usage is captured per assistant message, and a
context overflow triggers synchronous compaction plus one retry.

**Worth taking:** archive retention (deleting rows already covered by a summary and older than N days);
`cache_control` markers if an Anthropic backend appears.

**Deliberately not taking:** JSONL transcripts, a large context-engine interface.

## Skills and knowledge

**openclaw.** Skills are files on disk plus a plugin system with manifests; a community registry of 100+ skills;
automatic learning from history into skill drafts. Discovery is by name and metadata, and the prompt carries a
skill index.

**OctoForge.** Skills are rows found by embedding search, with per-user ownership, publication, versions and
usage counters, plus a declarative system slice synced at startup and a JSON overlay for per-installation
tuning. Nothing is loaded from disk at runtime and nothing is importable — capability is data.

**Different by design:** their model scales with a curated public registry; ours scales with retrieval quality
and per-user ownership. Theirs shares better; ours isolates better.

**Worth taking:** their notion of harvesting durable facts before compaction (a one-shot "extract what matters"
pass) — we compact without harvesting.

**Deliberately not taking:** a plugin manifest system, file watchers, a global skill registry in the prompt.

## Channels and delivery

**openclaw.** Channels are plugin packages implementing a ~25-surface contract (config, pairing, security,
groups, outbound, streaming, threading, auth, commands…). A shared draft-stream loop coalesces edits; Telegram
gets throttling, `retry_after` handling, preview aborts and minimum dwell before deletion. Access is an
allowlist plus pairing codes. Ack and status reactions (an emoji state machine), typing keepalive, media with
album handling and deduplication. A WebSocket JSON-RPC control plane with operator scopes; apps on every
platform.

**OctoForge.** One event stream, many renderers: a four-method surface rather than a contract with 25 of them.
Telegram support covers throttled drafts, per-exchange reply threading, tag-safe chunking, native Rich Messages
for tables and checklists, albums kept whole, voice transcribed as the user's own words, and an invite-based
access gate with member attribution. Multi-user is native rather than an allowlist over a single owner.

**Worth taking:** honouring `retry_after` explicitly; typing keepalive across long runs; status reactions
instead of text status lines; persisting the last delivered draft so a restart can re-render it.

**Deliberately not taking:** a WebSocket control plane, multi-account bots, the full channel-plugin contract.

## Security

**openclaw.** Exec approvals with modes (deny/allowlist/ask/auto/full), requests routed back into the
originating chat, allow-always persistence, safe-bin lists, an optional LLM reviewer, and a Docker sandbox (off
by default). A network policy blocking special-use and cloud-metadata addresses **with connection pinning to
validated addresses**, plus manual redirect handling that re-validates each hop. Secrets as references
(env/file/exec) with AES-256-GCM sentinels and a log-redaction registry. A metadata-only audit log with
pseudonymization, 30-day retention.

**OctoForge.** The attack surface is smaller by subtraction: no exec, so no approval modes, no sandbox, no
reviewer. Secrets are structurally outside the prompt path — the model sees codes, values are injected into
headers inside the call, bound to one host, and scrubbed from responses. Egress is guarded, redirects are never
followed, and one origin (our own API) is allowlisted by parsed origin.

**Their advantage, honestly:** connection pinning closes the DNS-rebinding gap we still document, and they have
an audit log where we have none. Both are in [../limitations.md](../limitations.md).

## Where each is stronger

**openclaw is stronger at:** breadth of channels and media, exec-based capability with a real approval model,
connection pinning, auditing, model failover, message-burst handling, and a curated public skill ecosystem.

**OctoForge is stronger at:** multi-user isolation as a schema property, concurrent obligations per conversation
with semantic routing, capability as owned searchable data, secrets that cannot reach the prompt, one
transactional store instead of file repair layers, and a much smaller surface for the same conversational job.

**The honest summary:** openclaw is a very capable assistant for the person who owns the machine. OctoForge is a
platform for giving many people an assistant that reaches into an organization's systems. Most of what is
valuable in openclaw is a portable idea rather than portable code.
