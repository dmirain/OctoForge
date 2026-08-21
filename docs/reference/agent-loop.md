# Agent loop

The reason-act cycle: model call, tool round, repeat until there is an answer. It is exposed as an
async iterator of typed events and knows nothing about dialogs, users or exchanges — those live one
layer up, in the [actor](conversation-actor.md).

## How it works

`AgentLoop.stream(history, control, context) -> AsyncIterator[LoopEvent]`. The caller passes a message
list, a cancellation flag and a `ToolContext`; the loop yields events and appends the run's own
messages (assistant turns and tool replies) to the list it was given.

One **iteration** is one streamed model call plus the tool executions it triggers. An iteration goes:

1. `IterationStarted(index)` is yielded. This is the synchronization point the actor uses to re-sync
   the narrative part of the history before the next model call reads it (the *pull model*).
2. The cancellation flag is checked; if set, the loop yields `Cancelled` and returns.
3. The model is streamed. Text arrives as `TextDelta`. A tool call whose arguments have finished
   streaming and parsed cleanly arrives as `ToolCallReady` — and is **started immediately**, before
   the assistant message is complete. Unparseable arguments arrive as `ToolCallBroken` and become an
   error reply for the model, with nothing executed.
4. Tool results land in a queue as they finish, in whatever order they finish, and are yielded as
   `ToolCallCompleted` / `ToolCallFailed` as soon as they land. Tool *messages appended to the
   history* are always written in call order, so the transcript is deterministic even though execution
   is concurrent.
5. If the assistant message carries no tool calls, the loop yields `Finished` and returns. Otherwise
   the next iteration starts.

Reaching `max_iterations` yields `Failed`.

### Eager, concurrent tool execution

Tools are not started after the message ends; they are started as their arguments become available,
and they run concurrently. Three 150 ms calls in one message therefore cost about 150 ms rather than
450 ms — measured in [../guides/performance.md](../guides/performance.md).

Providers that do not emit incremental tool-call events still work: if no incremental event was seen
during the stream, every call of the final message is spawned at the end of the iteration instead.

### Cancellation

`LoopControl` is a single flag with a waiter. The loop races that waiter against the next stream event
and against pending tool runs, which is what makes a stop button feel immediate:

- a silent provider does not hold the stop until the idle timeout;
- a long-running tool does not hold it either — remaining runs are aborted;
- the partial assistant text is kept: the interrupted message is appended with the tool calls that had
  arrived, and every one of them gets a reply, either its real result or `cancelled`. No orphaned tool
  call is left in the history, which would make the transcript unusable to the provider on the next
  turn.

### Errors are data

A tool that raises does not break the run: the exception is formatted (`format_error`, class name
included because some exceptions stringify to nothing) and returned to the model as
`error: <message>` tool output. The model can then correct itself. Only provider-level failures end a
run.

Transient provider failures are retried below the loop, by the retrying LLM client, and surface as
`RetryScheduled` events for transports that want to show "retrying" — see
[llm-clients.md](llm-clients.md).

### The idle watchdog

A stream that stops producing events for longer than `stream_idle_timeout` fails the run
(`Failed("LLM stream idle timeout")`) after aborting its tool runs. This covers providers that accept
a request and then go quiet — otherwise the run would hang forever holding a process slot.

## Events

| Event | Meaning |
|---|---|
| `IterationStarted(index)` | A new iteration begins; the re-sync point |
| `TextDelta(text)` | A streamed piece of the answer (transient, never persisted) |
| `AssistantMessage(message, interrupted, usage)` | The completed assistant turn |
| `ToolCallRequested(call)` | Execution of a call has started |
| `ToolCallCompleted(call, output)` | A call returned |
| `ToolCallFailed(call, error)` | A call failed; the error went to the model |
| `RetryScheduled(attempt, delay_seconds, reason)` | The client is retrying a transient failure |
| `Finished(message, usage, source_client_message_id)` | Terminal: the answer |
| `Cancelled()` | Terminal: stopped by the user |
| `Failed(error)` | Terminal: no answer produced |

`ProcessStarted` and `ProcessCompleted` are part of the same event union but are emitted by the actor,
not the loop.

## Invariants

- **Exactly one terminal event per run**: `Finished`, `Cancelled` or `Failed`.
- **The history the loop appends to is always provider-valid**: every assistant message with tool
  calls is followed by one tool reply per call, including on cancellation and on broken arguments.
- **Tool replies are in call order** regardless of completion order.
- **Deltas are transient.** Only whole messages are persisted, by the actor. A subscriber that misses
  deltas misses nothing durable.
- **The loop never reads the dialog store.** Everything it knows arrives as arguments; that is why it
  is reusable on its own (see [../guides/embed-the-core.md](../guides/embed-the-core.md)).
- **Tool visibility is resolved once per run**, from the `ToolContext` (`registry.specs(context)`),
  which keeps the tool list stable across a run's iterations — a requirement for provider prompt
  caching.

## Configuration

| Variable | Effect |
|---|---|
| `OF_AGENT_MAX_ITERATIONS` | Iteration cap per run (default 10). A backstop against loops, not a target |
| `OF_LLM_STREAM_IDLE_TIMEOUT_SECONDS` | Silence allowed between stream events (default 120, `0` disables) |
| `OF_LLM_MAX_RETRIES`, `OF_LLM_RETRY_BASE_SECONDS`, `OF_LLM_RETRY_MAX_SECONDS` | Retry policy of the client underneath |

## Failure modes

| Situation | Outcome |
|---|---|
| Provider stops sending events | `Failed("LLM stream idle timeout")`; tool runs aborted |
| Stream ends with no final message | `Failed("LLM stream ended without a final message")` |
| Iteration cap reached | `Failed("Agent loop reached the iteration limit")` |
| Tool raises | `ToolCallFailed` + `error: …` output to the model; run continues |
| Tool arguments are not valid JSON | `ToolCallBroken` → error output, no execution |
| Cancel mid-stream | Partial text kept, tool replies filled in, `Cancelled` |
| Cancel during a long tool call | Remaining runs aborted, their replies read `cancelled`, then `Cancelled` |

## Code anchors

- `core/src/octoforge_core/agent/loop.py` — the public `AgentLoop` coordinator and its config
- `core/src/octoforge_core/agent/loop_assistant.py`,
  `core/src/octoforge_core/agent/loop_stream_pump.py` — one streamed assistant turn and its races
- `core/src/octoforge_core/agent/loop_tools.py` — eager tool execution and ordered transcript writes
- `core/src/octoforge_core/agent/events.py` — the event union
- `core/src/octoforge_core/agent/control.py` — `LoopControl`
- `core/src/octoforge_core/llm/events.py` — the provider-level stream events the loop consumes
- `core/tests/test_agent_loop.py` — eager execution, cancellation, broken arguments, idle timeout
