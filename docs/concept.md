# The concept

OctoForge is an agent that a company runs for its own people. One installation serves many users,
learns things at runtime that nobody deployed, calls internal systems with credentials it is never
shown, and keeps working on something while its user is talking about something else.

This page explains the ideas that shape it, and the ones it deliberately refuses. Everything here is
implemented; where an idea has limits, the limits are named.

## The problem it addresses

An LLM becomes useful to an organization only when it can reach that organization's systems and
knowledge. There are two common ways to get there, and both have a cost that grows.

**Bake capability into the code.** Tools are functions, prompts are string constants, integrations
are modules. Every new thing the agent should know or do is a code change, a review and a deploy.
The people who know what the agent should do (support leads, analysts, operations) cannot express it;
the people who can express it (developers) do not have the context. Capability arrives at the speed
of the release process.

**Bolt a plugin system on.** Capability moves out of the source tree into descriptor files, plugin
packages or an MCP server. Better — but the unit of change is still an artifact somebody deploys and
a process somebody restarts, and the descriptors are global: one configuration for everyone,
per-user knowledge does not fit.

OctoForge takes a third position: **capability is data, owned by users, found by search.**

A skill is a row. So is a piece of knowledge, the contract of an HTTP endpoint, the description of a
dataset, and a personal memory. Rows have owners: private to one user, or public to the
installation. At request time the agent searches them by meaning and pulls in what is relevant. The
agent writes rows itself when it learns something worth keeping; an operator can promote a private
row to everyone.

Adding an integration to an internal CRM means storing an endpoint record that names the URL
template, the parameter schema and which stored secret authenticates it. No deploy, no restart, no
code review — and a user can do it for themselves without affecting anyone else. The mechanics are
in [reference/instructions.md](reference/instructions.md) and
[reference/endpoints-and-net.md](reference/endpoints-and-net.md).

The MCP ecosystem plugs into the same position rather than the rejected one: an external MCP server
becomes a record (`mcp_add`), its tools become endpoint records found by search, and nothing global
enters any prompt. What the plugin stance was rejected for — deployed artifacts, one descriptor list
for everyone — stays rejected; see [reference/mcp.md](reference/mcp.md).

The trade is explicit: retrieval quality now matters as much as prompt quality. If search does not
surface the right record, the agent behaves as if the capability did not exist. That is why ranking
is a first-class concern — cosine over embeddings, an exact-title boost, an optional cross-encoder
rerank — and why the tool that searches (`recall`) is described in the prompt as the first move for
any non-trivial request.

## A dialog is an actor, not a request handler

Most chat systems are request/response: a message arrives, a run produces an answer, the next
message waits. That model breaks in the situations that actually happen at work. Someone asks a
question, then immediately adds a correction. Someone asks a second, unrelated question while the
first answer is still being written. A scheduled job produces something at 9:00 while its owner is
mid-conversation. A run needs to ask the user which of two accounts they meant, and cannot proceed
until they say.

So a dialog here is a long-lived actor (one per `(user_id, channel)` pair) with two things in it:

- a **narrative** — the durable, append-only story of the dialog: user messages, finished answers,
  system notices. It lives in the database and survives restarts.
- **processes** — in-memory runs, each backed by a task row. A process is either answering an
  obligation to the user or doing deferred work.

There is no foreground. Every answer run streams concurrently, and each event it emits is tagged
with the obligation it belongs to, so a transport can keep one message bubble per obligation instead
of interleaving two answers into one. See
[reference/conversation-actor.md](reference/conversation-actor.md).

### Obligations are durable objects: the exchange

The central noun is the **exchange**: one obligation the installation owes a user — their question,
its clarifications, and eventually the answer. It is a database row with a lifecycle
(`OPEN → IN_PROGRESS → ANSWERED | AWAITING_USER | CANCELLED | FAILED`), and messages point at it.

Making the obligation durable, rather than implicit in whichever run happens to be alive, buys three
properties that are otherwise very hard:

- **Restart safety.** "What does this installation still owe people?" is a SQL predicate, not an
  inspection of process memory. After a crash, open exchanges are picked up again.
- **Parallelism without confusion.** Several obligations can be in flight; each run knows which one
  is its own and treats the others' questions as none of its business.
- **Waiting is a state, not a stuck process.** When a run calls `ask_user`, its exchange is parked in
  `AWAITING_USER` and the run finishes. The user's reply starts a fresh run that continues the same
  obligation. Nothing is blocked meanwhile, and a nudge re-asks if the user goes quiet.

An exchange is not a task. A task is a unit of work; an exchange is a promise. A task can succeed
while the promise stays open (because the run asked a question), and one promise can be served by
several tasks over time. See [reference/exchanges.md](reference/exchanges.md).

### Whose message is this?

If several obligations can be open at once, an arriving message is ambiguous: it might clarify one of
them, start a new one, or ask to stop one. A small LLM call decides, over a snapshot of the live
exchanges — and the decision is skipped entirely when the transport already knows (a Telegram reply
resolves deterministically to the exchange it replies to), and defaults to "new obligation" on any
doubt, timeout or failure. Guessing "new" costs a redundant answer the user can see and correct;
guessing "clarification" feeds the message into someone else's run where it may never be answered.
See [reference/routing.md](reference/routing.md).

### Not everything is a question

Forwarded material — an article, a thread, a screenshot someone shares — is not a request. Treating
it as one produces an answer per forward, which is exactly wrong when someone dumps six messages in
a row. Material accumulates in a single `COLLECTING` exchange; when the burst falls quiet a sweep
hands the collection to the dialog, which reacts once. If a real question arrives first, it adopts
the collection instead. Forwarded text never carries authority: it cannot cancel exchanges, and the
branch marks it as somebody else's words.

## The core is a library, the surfaces are adapters

`octoforge-core` is a plain typed Python package. It never imports a web framework. Everything
external — the model, embeddings, storage, HTTP, scheduling, transports — sits behind a `Protocol`
port, and nothing inside constructs its own dependencies: one composition root assembles the object
graph and hands it in.

The two surfaces in this repository (a streaming web chat and a Telegram bot) are adapters over the
same conversation engine and the same typed event stream. A third one — an intranet portal, a
ticketing system, a voice gateway — is a subscriber to that stream plus a renderer. That is the
"embeddable" claim in concrete terms: [architecture.md](architecture.md) and
[guides/embed-the-core.md](guides/embed-the-core.md).

The same discipline makes optional features honest. Vision, speech, web search, the reranker, the
secret store and the Telegram bot are each a port with a switch: no configuration, no feature, no
half-working stub. What is on is printed at startup as a capability report, because a silently
disabled feature is worse than a missing one.

## Multi-user is in the schema

Isolation is not a layer on top; it is a predicate in every query. A private record belongs to its
owner, a public record belongs to the installation, and saving over a public record creates the
saver's personal shadow copy rather than mutating everyone's. Dialogs are keyed by
`(user_id, channel)`, and per-user secrets are encrypted with a master key the model never touches.

There is exactly one place that reads across users: the operator console's read model, which is
read-only. Mutations always go through owner-scoped services.

Be precise about what this does and does not give you today. Within the agent's own surfaces,
per-user isolation holds. The HTTP surface, however, authenticates *the operator*, not your
employees: `X-User-Id` selects the dialog and is a trusted string, so the deployment expects an
authenticating proxy in front of it. Telegram is different — there each user is identified by the
messenger and gated by an invite system. See [security.md](security.md).

## What it refuses to be

These are decisions, not gaps, and they are what makes the rest of the design affordable.

**No shell, no filesystem tools.** The agent acts through declared, schema-validated HTTP contracts.
An agent that can run commands needs an approval model, a sandbox, an audit trail and a policy
language around them — a large machine whose failure modes are severe in a multi-user deployment.
Removing the capability removes the machine. The cost is real: OctoForge cannot fix a server or edit
a repository. It is not a coding agent, and it is not trying to be.

**No agent-to-agent framework.** There are no graphs, no crews, no role-playing sub-agents. The unit
of concurrency is a process serving an obligation, and the unit of capability is a stored record.
Orchestration complexity has to justify itself against those two, and mostly it does not.

**No vendor coupling.** Any OpenAI-compatible endpoint works, including a local one; embeddings and
reranking have both in-process and HTTP backends. Nothing in the core knows which provider it is
talking to.

**No cleverness in the hot path.** One asyncio process serves every dialog, so anything whose cost
grows with data must not run inline: ranking is vectorized and moved to a worker thread, streaming
deltas are never persisted, and a slow subscriber loses stream events but never terminal ones. This
is a standing rule with measured cases, not an aspiration —
[guides/performance.md](guides/performance.md).

**No history in the interface.** Prompts, tool descriptions and stored skills say what to do now.
The same applies to this documentation.

## How to read the rest

[glossary.md](glossary.md) defines the vocabulary — worth two minutes, because words like *exchange*,
*narrative*, *material* and *process* have precise meanings here. [architecture.md](architecture.md)
shows the shape of the system and the seams. The `reference/` pages then describe each aspect on its
own terms, and [limitations.md](limitations.md) collects everything this design does not currently
do.
