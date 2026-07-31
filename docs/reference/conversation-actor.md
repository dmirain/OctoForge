# The conversation actor

One object owns everything about one dialog: its durable narrative, its exchanges, the processes
running inside it, and the delivery of their results. It is the only place dialog state is mutated,
and it does that from a single serialized command inbox.

## How it works

`ConversationManager` maps `(user_id, channel)` to a `ConversationRunner` — the actor —
creating it on first contact and rebuilding its narrative from the database. `ConversationRunner`
runs one asyncio task consuming an inbox of commands (`_Submit`, `_Flush`, `_PromoteCollected`,
`_ProcessTerminated`), so no two commands touch the dialog's state at the same time.

### Narrative and branches

The **narrative** is the dialog's durable story: user messages, run finals, broker notices. It is
append-only, persisted, and reloaded on startup — but only its hot slice, from the compaction
boundary onward; everything older is reachable through summaries and `history_search`.

Each process gets a **branch**: `[system prompt] + narrative snapshot (with role marks) + its own
private working suffix` (the assistant turns and tool replies of this run). At every iteration
boundary the branch re-syncs its narrative part from the actor — the **pull model**. A message that
arrives mid-run therefore lands in the narrative exactly once and is seen by every live process,
without an injection channel and without duplicating text into branches.

Marks are applied to the branch copy only, so the stored narrative keeps clean text. The rules live in
one place (`agent/branch.py`):

- the run's own question is marked as its task;
- later messages of the same exchange are marked as clarifications of it;
- forwarded material of the run's own exchange is marked as material — somebody else's words the user
  shared, never an instruction;
- forwarded material of *other* live exchanges is kept, marked as shared background;
- questions of *other* live exchanges are **dropped**: they are someone else's obligation, and a
  question left visible pulls the model into answering it even when told not to;
- everything else (answers, notices) is plain history.

The current date and time ride as an envelope on the branch's last message rather than in the system
prompt, which keeps the prompt prefix byte-stable for provider caching. The narrative and the stored
copy never contain the envelope.

### Processes

A **process** is a run inside the actor, always backed by a task row:

- an **answer run** (`TaskKind.ANSWER`) satisfies one exchange. It streams live, and every event it
  emits carries its `exchange_id`.
- a **RUN task** (`TaskKind.RUN`) is deferred work from `task_create` or a cron firing. Its branch is
  self-contained (it never re-syncs the narrative), it produces no live stream for the user, and its
  result is delivered whole when it finishes.

`OF_MAX_PROCESSES` caps how many exist at once per dialog. Exceeding it is answered with a templated
broker notice, not a silent drop.

There is no foreground: several answer runs stream at the same time, each into its own exchange. A
transport keeps one draft or bubble per exchange, so concurrent answers never mix.

### Event delivery

Subscribers call `subscribe()` and receive `ConversationEvent(dialog_id, seq, exchange_id, payload)`
where the payload is a loop event. Two markers come from the actor rather than the loop:
`ProcessStarted` before the first token of an answer (so a transport that threads replies can create
the message with the right reply target) and `ProcessCompleted` when a process reaches a terminal
status.

Broadcast is deliberately asymmetric under back-pressure. Queues are bounded; when one is full,
stream events (`TextDelta`, tool events) are dropped for that subscriber, while critical events —
terminals and process markers — evict the oldest entry and are always enqueued. A slow or reconnecting
client may lose tokens; it does not lose the answer.

### Delivery and the outbox

Whether a result reached anyone is tracked, not assumed:

- an answer run has already streamed into its exchange's message, so only `delivered_at` on the task
  row is stamped — and only if the terminal event was accepted by at least one subscriber queue;
- a RUN task's result (or a broker notice) goes into the outbox and out whole — `TextDelta` +
  `Finished`, or `Failed` — as soon as a subscriber is attached. With none attached, it waits; the next
  `subscribe()` enqueues a flush command that drains it.

The outbox is retention for the no-subscriber case, not a replay log: live stream events are never
replayed. An `ask_user` question jumps the queue, and drained entries are removed by identity rather
than position so a question inserted at the head mid-flush cannot displace another delivery.

### Cancellation

`cancel()` does not go through the inbox. The actor may be busy inside a router call for up to
`OF_ROUTER_TIMEOUT_SECONDS`, and a stop that queued behind it would feel broken. Instead it directly
trips the `LoopControl` of every live answer run, and closes `AWAITING_USER` exchanges whose owning
run is no longer alive — otherwise the nudge would keep re-asking a question the user just stopped
waiting for. RUN tasks are not touched: `task_delete` stops those.

### Restart recovery

`ConversationManager.recover_interrupted()` runs before the scheduler and the surfaces start, over
the dialogs **no live process owns** — see [dialog-ownership.md](dialog-ownership.md):

1. `PENDING`/`RUNNING` task rows are orphans of the previous process — they are restarted as
   background processes (respecting the per-dialog limit; exceeding it marks the row failed and
   delivers a `Failed`).
2. `IN_PROGRESS` exchanges are reopened, and `OPEN` exchanges with no owner are swept and resumed —
   both at startup and whenever a process slot frees up.
3. Task rows that finished but were never delivered (`delivered_at IS NULL`) are re-delivered through
   the normal terminal path, which is idempotent.

Every step is scoped to one dialog, and a dialog whose claim is fresh and held by a different owner
is skipped entirely. The steps themselves run when the dialog's runner is built, so a process that
*takes a dialog over* recovers it exactly as a restarting one does — see
[dialog-ownership.md](dialog-ownership.md).

### Surfaces

A dialog whose channel has a transport gets it when its **actor is built**, not when a request
arrives: a scheduled run finishing at four in the morning still has to reach the user, and a dialog
that has just moved here from another process has nobody else left to deliver it. The transport is
dropped again when the dialog is evicted, shut down, or taken over elsewhere.

Core knows only the `DialogSurface` port — attach and detach, nothing about chats. Which channel gets
which transport is the composition root's decision, and a surface that fails to attach costs delivery
through that transport, never the dialog.

## Invariants

- **One actor per dialog, one task consuming its inbox.** Dialog state is never mutated concurrently.
- **A message is persisted before it is routed or answered**, so a crash cannot lose it while an
  answer is in flight.
- **The narrative is append-only.** Nothing is edited or reordered; compaction replaces a prefix with
  a summary and shifts process watermarks by exactly the number of trimmed messages.
- **The watermark comes from the compactor's snapshot**, not from the live narrative length at
  return time, and branch assembly is serialized per actor. A message appended during assembly is
  either above the watermark (the next sync sees it) or already in the branch — never silently lost.
- **Marks and the date envelope exist only in branch copies.**
- **A terminal is stamped delivered only when at least one subscriber accepted it**; otherwise it goes
  to the outbox.
- **Cancellation bypasses the inbox** and therefore cannot queue behind slow work.
- **Every process has a task row**, which is what makes restart recovery a query rather than a guess.
- **A dialog's transport is bound to its actor**, so delivery does not depend on anyone watching.
- **An actor answers only while it owns its dialog.** A run checks the claim before it starts, and a
  preempted actor stands down instead of finishing — see
  [dialog-ownership.md](dialog-ownership.md).

## Configuration

| Variable | Effect |
|---|---|
| `OF_MAX_PROCESSES` | Concurrent processes (hence live exchanges) per dialog |
| `OF_ROUTER_TIMEOUT_SECONDS` | How long routing may take before defaulting to a new exchange |
| `OF_MATERIAL_QUIET_SECONDS` | Quiet window before a material collection earns its own reaction |
| `OF_CONTEXT_HOT_MAX_CHARS`, `OF_CONTEXT_COMPACT_TARGET_CHARS`, `OF_MODEL_CONTEXT_TOKENS`, `OF_CONTEXT_BUFFER_TOKENS` | When and how hard the narrative is compacted |

## Failure modes

| Situation | Outcome |
|---|---|
| Subscriber too slow | Its stream events are dropped; terminals and markers still delivered |
| No subscriber at all when a RUN task finishes | Result waits in the outbox, delivered on the next `subscribe()` |
| Process slot limit reached | Templated broker notice to the user; nothing is silently dropped |
| Crash with runs in flight | Task rows restarted, exchanges reopened, undelivered results re-sent |
| Another process takes the dialog | The actor stands down: streams close so clients reconnect, and the in-flight work is left for the new owner's recovery |
| A command handler raises (e.g. the router throws) | The actor answers that submit with `Failed`, stays alive, and keeps serving the dialog |
| Provider context overflow mid-run | Reactive compaction, then the run is retried once (see [context-compaction.md](context-compaction.md)) |

## Code anchors

- `core/src/octoforge_core/agent/runner.py` — `ConversationRunner`, `ConversationManager`, `_Process`,
  `DialogSurface`, the inbox commands and the outbox
- `core/src/octoforge_core/agent/branch.py` — branch rendering and role marks
- `core/src/octoforge_core/dialogs/api.py`, `core/src/octoforge_core/dialogs/store.py` — dialog,
  message and exchange persistence
- `core/tests/test_conversation_runner.py` — actor behavior, delivery, recovery, cancellation
- `core/tests/test_branch.py` — the mark rules
