# Routing

When several obligations can be open at once, an arriving message is ambiguous: it may clarify one of
them, start a new one, or be pure control ("stop that"). Routing answers one question — *whose is this
message?* — and answers it safely when it cannot tell.

## How it works

The actor resolves as much as it can without a model call, and only then asks:

1. **The transport already knows.** If the message came with `reply_to_exchange_id` (a Telegram reply,
   or a web client that tracks bubbles), it belongs to that exchange. No LLM call.
2. **Nothing is live.** With no live exchanges in the dialog there is nothing to belong to: a new
   exchange is the only possible answer. No LLM call.
3. **Otherwise the `MessageRouter` port is asked.** The default implementation (`LLMRouter`) makes one
   short tool call whose entire input is a list of one-line descriptions of the live exchanges plus the
   incoming text.

The decision is a `RouteDecision`:

| Field | Meaning |
|---|---|
| `action` | `NEW` (its own exchange), `CONTINUE` (belongs to an existing one), `COMMAND` (pure control, nothing to answer) |
| `exchange_id` | The target of a `CONTINUE` |
| `cancel_ids` | Exchanges the user explicitly asked to stop |

Each live exchange is described to the model with its id, title, state in plain words ("being answered
right now", "waiting for the user to reply", "material the user forwarded, not answered yet"), its age,
and — when it is waiting — the question the agent asked. That is deliberately all: the router sees a
list of obligations, not the dialog history, which keeps the call small and cheap.

### Safety by default

Every failure path degrades to `NEW`:

- the call times out (`OF_ROUTER_TIMEOUT_SECONDS`),
- the provider errors,
- the answer carries no `route` tool call,
- the action is not a known value,
- a `CONTINUE` names an unknown exchange id.

That direction is chosen on purpose. A wrong `NEW` costs a redundant answer the user can see and
correct. A wrong `CONTINUE` feeds the message into someone else's run, where it may never be answered
at all.

`cancel_ids` are filtered against the ids actually shown to the router, so a hallucinated id cannot
stop anything. Cancellations derived from *forwarded material* are ignored entirely — forwarded text is
untrusted input, not an instruction from the user.

### What the actor does with the decision

- `NEW` → open an exchange and start an answer run, unless the per-dialog limit is reached, in which
  case the exchange stays `OPEN` and is picked up when a slot frees.
- `CONTINUE` → attach the message to that exchange. If its run is alive, the pull model means the run
  sees the message at its next iteration; if the exchange was `AWAITING_USER` or `OPEN`, a fresh run
  continues it.
- `COMMAND` → nothing to answer; only the cancellations apply.
- A `COLLECTING` target means the question adopts the forwarded material (see
  [exchanges.md](exchanges.md)).

## Invariants

- **A message belongs to exactly one exchange.** There is no notion of a message shared by two
  obligations, which is why no conflict resolution is needed.
- **Deterministic paths never spend an LLM call**: an explicit reply, or a dialog with nothing live.
- **Every ambiguity, error or timeout resolves to `NEW`.**
- **`cancel_ids` are validated against the exchanges shown to the router.**
- **Routing failure never breaks the dialog**: the router catches everything and returns a decision.
- **The router's prompt is a template** with `{limit}` and `{exchanges}` placeholders, provided through
  the `PromptProvider` port, so a deployment can replace the wording without touching code.

## Configuration

| Variable | Effect |
|---|---|
| `OF_ROUTER_TIMEOUT_SECONDS` | Timeout of the routing call (default 10 s); on timeout the message opens a new exchange |
| `OF_MAX_PROCESSES` | Passed to the router as the limit, and enforced by the actor |
| `OF_ROUTER_PROMPT_SOURCE` | `file:` override of the router prompt, re-read every turn |

## Failure modes

| Situation | Outcome |
|---|---|
| Provider slow or down | Timeout → new exchange; the user gets an answer, possibly a redundant one |
| Model returns an unknown action or id | Logged, treated as new exchange |
| Model asks to cancel an exchange the user never mentioned | Only ids from the shown list can be cancelled; forwarded-material batches cannot cancel anything |
| Router misclassifies a clarification as new | An extra answer appears in its own exchange; the user can stop it |
| Limit reached with a `NEW` decision | The exchange waits as `OPEN`, and the user is told when the attempt was theirs |

## Code anchors

- `core/src/octoforge_core/agent/router.py` — `MessageRouter`, `LLMRouter`, `RouteDecision`,
  `ROUTE_TOOL_SPEC`, the fallbacks
- `core/src/octoforge_core/agent/runner.py` — the deterministic paths and what happens to a decision
- `core/src/octoforge_core/agent/prompts.py` — the router prompt template
- `core/tests/test_router.py` — parsing, validation and fallback behavior
