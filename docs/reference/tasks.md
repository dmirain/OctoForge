# Tasks

A task is the durable record of one unit of work: what it was, how it ended, and whether its result
reached anyone. Every process inside a dialog is backed by one, which is what lets work survive a
restart and lets delivery be proven rather than assumed.

## How it works

A task row carries the dialog and user it belongs to, a title, a kind, a status, its input, its result
or error, and four timestamps (`created_at`, `started_at`, `finished_at`, `delivered_at`).

### Kinds

| Kind | What it is | Visible to the agent |
|---|---|---|
| `ANSWER` | The internal mechanics of answering a user message — one per answer run | No: `task_list` hides them |
| `RUN` | Deferred work created by `task_create`, or a cron firing | Yes |

### Statuses

`PENDING → RUNNING → DONE | FAILED | CANCELLED`. Rows are **kept forever**, terminal states included:
they are the audit trail of what the installation did, and the source of restart recovery.

### The obligation is a column; the input keeps the rest of the parent link

`exchange_id` is a real column with a foreign key to `exchanges`, NULL for `RUN` tasks — cron and
spawned work owe the user nothing. It used to live only inside the `input` JSON, where nothing could
join on it, index it or check it; the two composite indexes that make recovery cheap
(`(exchange_id, status)` and `(dialog_id, status)`) exist because of it.

`input` still records what the run was given, so a restart can reconstruct it:

- an answer task: `{source_message_id, exchange_id}` — the user message being answered and the
  obligation it owes (the same id as the column: a run's input is not rewritten after the fact);
- a cron firing: `{cron_job_id, fired_at}`.

### Agent-facing tools

| Tool | Behavior |
|---|---|
| `task_create(title, prompt[, schedule, timezone, one_shot])` | Without `schedule`: background work started now. With `schedule`: a cron job (delegated to the cron domain). The prompt must be self-contained — the dialog's context may be gone when it runs |
| `task_list()` | RUN tasks of this dialog that are in flight or awaiting delivery, plus the user's cron jobs with next fire times. Delivered results are not repeated — they are already in the conversation |
| `task_delete(id)` | Stops a live process, or removes a cron job. Stopping leaves the row as `CANCELLED`; it never deletes history |

A tool cannot delete the task it is itself running in — that is rejected using
`ToolContext.owner_task_id`, because the pump cannot be awaited from inside itself.

### Delivery

Delivery is bookkeeping, not hope:

- an **answer run** has already streamed into its exchange's message, so its terminal only needs
  `delivered_at` — stamped when at least one subscriber accepted the terminal event;
- a **RUN task** result is delivered whole: `TextDelta` with the text, then `Finished` (or `Failed`).
  If no subscriber is attached, it waits in the actor's outbox and goes out on the next `subscribe()`.

No LLM call is involved in delivery. There is no "report run" that re-describes a finished task.

### Restart recovery

`ConversationManager.recover_interrupted()` uses the task table as the source of truth:

1. `list_orphaned()` — every `PENDING`/`RUNNING` row is an orphan of the previous process. Each is
   restarted as a background process; if the dialog is at its process limit, the row is marked failed
   and a `Failed` is delivered rather than being silently lost.
2. `list_undelivered()` — terminal rows with `delivered_at IS NULL` are re-delivered through the normal
   path, which is idempotent on `delivered_at`.
3. Cron-tagged outcomes are reported to the `TaskOutcomeListener` when the restarted process finishes,
   so a job's `last_status` reflects reality.

## Invariants

- **Every process has a task row**, created before the process starts.
- **Rows are never deleted by the agent.** `task_delete` cancels; only administrative dialog deletion
  removes rows.
- **`delivered_at` is only stamped when delivery actually happened** — for streamed answers, when a
  subscriber accepted the terminal; for background results, when the outbox flushed.
- **`ANSWER` tasks are invisible to tools.** The agent reasons about obligations (exchanges), not about
  the mechanics of its own replies.
- **A cancelled task stays `CANCELLED`**; it is never redelivered.
- **`input` is the only parent link**, so recovery needs no in-memory state.
- **Task creation is capped** by the per-dialog process limit; the refusal is a message to the user, not
  an exception.

## Configuration

| Variable | Effect |
|---|---|
| `OF_MAX_PROCESSES` | How many processes (and therefore concurrent tasks) a dialog may have |

Cron-specific settings are in [cron.md](cron.md).

## Failure modes

| Situation | Outcome |
|---|---|
| Crash while a task is `RUNNING` | Restarted at startup from the row |
| Crash after a task finished but before delivery | Re-delivered at startup |
| Process limit reached at recovery time | Row marked failed, a `Failed` delivered — the user learns about it |
| Tool tries to delete its own task | Rejected with a clear error |
| Task fails | Row `FAILED` with the error text; the user receives `Failed` |
| No subscriber when a RUN task finishes | Result waits in the outbox; nothing is lost |

## Code anchors

- `core/src/octoforge_core/tasks/api.py` — `Task`, `TaskKind`, `TaskStatus`, errors
- `core/src/octoforge_core/tasks/store.py` — the `TaskStore` port, SQL and in-memory implementations
- `core/src/octoforge_core/tasks/tools.py` — `task_create`, `task_list`, `task_delete`
- `core/src/octoforge_core/tools/base.py` — the `TaskSpawner` / `TaskDeleter` ports the actor binds
- `core/src/octoforge_core/agent/runner.py` — process lifecycle, delivery, recovery
- `core/tests/test_tasks.py`, `core/tests/test_conversation_runner.py` — behavior
