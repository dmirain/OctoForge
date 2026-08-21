# Adding a surface

A surface is an adapter: it turns incoming messages into `submit()` calls and turns the dialog's event stream
into whatever its medium shows. The web chat and the Telegram bot are two of these; a third one is the same
two functions.

## The contract

```python
runner = await manager.get_or_create_runner(user_id, channel)
events = runner.subscribe()  # subscribe BEFORE submitting, or you lose the first events
await runner.submit(
    DialogSubmission(
        text,
        client_message_id=...,
        reply_to_exchange_id=...,
        source=...,
    )
)

while True:
    event = (
        await events.get()
    )  # ConversationEvent(dialog_id, seq, exchange_id, payload)
    render(event)
```

That is the whole coupling. Everything else — obligations, concurrency, persistence, delivery — is the actor's
job.

| You call | For |
|---|---|
| `get_or_create_runner(user_id, channel)` | Pick the dialog. Choose a stable `channel` string for your surface |
| `subscribe()` / `unsubscribe(queue)` | Attach and detach. Attaching also drains anything waiting in the outbox |
| `submit(DialogSubmission(...))` | Deliver one typed user message |
| `cancel()` | Stop the dialog's live answer runs |

## Four things a good surface gets right

**1. One target per exchange.** Every event carries `exchange_id`. Concurrent answers are normal here, so keep
one bubble, draft or thread per exchange and append deltas to the right one. Interleaving them into a single
stream produces unreadable output.

`ProcessStarted` arrives *before* the first token precisely so a medium that must choose a reply target at
creation time (Telegram) can create the message first.

**2. Pass an idempotency key.** `client_message_id` makes a retried delivery harmless — the actor accepts and
skips a message it has already recorded. Any transport with at-least-once delivery needs this.

**3. Resolve replies yourself when you can.** If your medium knows what the user replied to, pass
`reply_to_exchange_id`: the routing LLM call is skipped entirely. This is both faster and more accurate than
letting the router infer it.

**4. Say which kind of message it is.** A forward or a bare image is `MessageKind.MATERIAL`, not a question —
it must not open an obligation. A voice message is the user speaking, so it is an ordinary user message. Only
the transport knows the difference; the core trusts your classification.

## Rendering the events

| Event | Typical rendering |
|---|---|
| `ProcessStarted` | Create the message/bubble for this exchange |
| `TextDelta` | Append (throttle edits if your medium charges for them) |
| `ToolCallRequested` / `ToolCallCompleted` / `ToolCallFailed` | Optional activity indicator |
| `RetryScheduled` | "Retrying…" instead of a frozen cursor |
| `Finished` | Replace the draft with the final text |
| `Failed` | Show the error; the obligation is closed |
| `Cancelled` | Mark the partial answer as stopped |
| `ProcessCompleted` | Clear indicators for that process |

Deltas are transient: a subscriber that falls behind loses them, while terminals are always delivered. Do not
build state that depends on having seen every delta.

## Backpressure and reconnects

Subscriber queues are bounded. If your renderer is slow, its stream events are dropped and the terminals still
arrive — so a reconnecting client sees a complete answer, not a broken one. On reconnect, `subscribe()` also
flushes results that finished while nobody was attached (background tasks, cron firings).

Nothing is replayed. If your medium needs scrollback, read the persisted messages
(`MessageRepository` / the admin read model), not the event stream.

## Long-running and scheduled work

You get those for free: a RUN task's result and a cron firing arrive on the same stream, with
`exchange_id = None` and delivered whole (`TextDelta` + `Finished`). Render them as a new message rather than
appending to whatever bubble happens to be last.

## Checklist

- A stable `channel` string, and a `user_id` scheme that is stable per person (`tg:<id>` is the Telegram
  example).
- Subscribe before the first submit; unsubscribe on disconnect.
- One rendering target per `exchange_id`.
- `client_message_id` on every submit.
- `reply_to_exchange_id` whenever the medium knows it.
- Material classified as material.
- Terminals always handled; deltas treated as optional.
- Your own rate limits respected (throttle edits, chunk long text).

## Code anchors

- `server/src/octoforge_server/api/dialog.py`, `server/src/octoforge_server/api/sse.py` — the smallest complete surface
  (REST + SSE)
- `surfaces/telegram/src/octoforge_telegram/bridge.py` — the thorough one: drafts, throttling, reply threading, chunking
- `surfaces/telegram/src/octoforge_telegram/poller.py` — ingestion, per-user queues, message-kind decisions
- `core/src/octoforge_core/agent/runner.py` — the stable actor API facade
- `core/src/octoforge_core/agent/runner_api.py` — `DialogSubmission` and `ConversationEvent`
- `core/src/octoforge_core/agent/runner_facade.py` — `subscribe`, `submit` and `cancel`
- [../reference/conversation-actor.md](../reference/conversation-actor.md) — delivery guarantees in detail
