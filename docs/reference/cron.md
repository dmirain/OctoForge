# Cron

Scheduled prompts. A cron job wakes its owner's dialog with a self-contained instruction, which then
runs through the same machinery as any other background work — nothing about the agent's execution path
is special-cased for schedules.

## How it works

A job belongs to exactly one user and carries a cron expression, an IANA timezone, the prompt to run,
and bookkeeping: `next_fire_at`, `last_fire_at`, `last_status`, `last_error`, an enabled flag and a
one-shot flag.

The timezone matters: the schedule defines a *wall-clock* cadence, so "every morning at 8" means the
user's local morning, while every stored timestamp is aware UTC.

### The loop

`CronScheduler` polls every `OF_CRON_POLL_INTERVAL_SECONDS`:

1. `list_due()` — jobs whose `next_fire_at` has passed.
2. `claim(job, owner, lease_ttl)` — a SQL compare-and-swap that records who is firing it. Only one
   claimant wins, so several instances can run schedulers against one database; a claim from a process
   that died expires after `OF_CRON_LEASE_TTL_SECONDS` and is reclaimed by a live one.
3. The job fires through the `CronWaker` port — implemented by `ConversationManager.wake()`, which
   creates a RUN task in the owner's dialog and starts a process for it. It answers with a
   `WakeOutcome`, and only `DELIVERED` is a fire: `LIMITED` (the dialog is at its process limit)
   keeps the claim so the job retries once the lease goes stale, and `NOT_OURS` (a live peer owns
   the dialog) releases it immediately so that peer fires it — winning a lease must not move a
   conversation, see [dialog-ownership.md](dialog-ownership.md).
4. `complete_fire()` — `next_fire_at` is recomputed *from the fire time*, and `last_fire_at` is set.

### Missed firings are coalesced, not replayed

If the process was down (or the machine asleep), a job may be overdue by many periods. Instead of a
burst of catch-up runs, the scheduler fires **once** and appends the missed count to the prompt
("N scheduled runs were missed and coalesced into this one"), then recomputes the next time from the
fire moment. Catching up is additionally paced by a small stagger per job that missed runs, and bounded
by `OF_CRON_REPLAY_LIMIT` firings per tick.

Jobs that are merely due at the same on-time tick are not staggered — that is not a downtime burst.

### Outcomes

The dialog side reports every cron-tagged task's terminal status back through the `TaskOutcomeListener`
port; `CronOutcomeReporter` applies the policy:

- `DONE` — recorded; a one-shot job is deleted, a recurring one keeps its schedule;
- `FAILED` — recorded as `last_status`/`last_error` (truncated); a one-shot job is still deleted — one
  shot means one attempt;
- `CANCELLED` — recorded the same way; the user stopped it on purpose.

**No outcome ever reschedules a job.** There is no retry: a failure is visible in `last_status` and in
the console, and the next regular firing happens on time.

### Agent-facing surface

Cron jobs are created through the task tools — `task_create` with a `schedule` — so the agent has one
mental model for deferred work. `task_list` shows jobs with their next fire times; `task_delete`
removes them; `cron_pause` and `cron_resume` toggle the enabled flag. Duplicate creation (same
schedule, same prompt) is deduplicated.

The HTTP API exposes the same jobs for operators (`/api/cron`), and the console can enable or disable
them.

### Replacing the engine

`Scheduler` is a port. Another engine (APScheduler, Celery beat, OS cron) plugs in two ways: implement
the port and start it instead of `CronScheduler`, or drive the public firing contract from outside —
`list_due` / `claim` / `release_claim` / `complete_fire` plus `compute_next_fire` / `count_missed`.
Outcomes always flow back through `record_fire_result`.

## Invariants

- **A job is owned by one user**, and firing enters that user's dialog only.
- **Claiming is atomic.** Two schedulers cannot fire the same job for the same due time.
- **Winning a lease does not move the dialog.** The job is handed back to the instance that owns it.
- **A lease expires**, so a crashed instance does not freeze a job forever.
- **`next_fire_at` is computed from the fire time**, never from "now plus period", so downtime cannot
  shift a schedule permanently.
- **Missed runs coalesce into one firing** carrying the count.
- **Failures are never retried automatically.**
- **A one-shot job is deleted after its single attempt**, whatever the outcome.
- **A firing is a normal RUN task**: same process machinery, same delivery, same recovery on restart.
- **Schedule and timezone are validated on creation** (`CronScheduleError`), so a broken expression
  cannot enter the store.

## Configuration

| Variable | Effect |
|---|---|
| `OF_CRON_POLL_INTERVAL_SECONDS` | How often due jobs are looked for (default 1 s) |
| `OF_CRON_LEASE_TTL_SECONDS` | Claim lease (default 60 s) — the window after which a dead owner's claim is reclaimed |
| `OF_CRON_REPLAY_LIMIT` | Maximum firings per tick while catching up (default 5) |

## Failure modes

| Situation | Outcome |
|---|---|
| Instance dies mid-firing | The claim expires; another instance (or the same one after restart) fires it. The RUN task itself is recovered by task recovery |
| Long downtime | One coalesced firing per job, with the missed count in the prompt |
| Job prompt depends on dialog context that is gone | The run does the wrong thing — hence the tool description insists prompts be self-contained |
| Firing fails | `last_status=failed`, `last_error` recorded; no retry; next regular firing proceeds |
| Dialog at its process limit when a job fires | The firing is refused and reported as such; the schedule is untouched |
| The winning instance does not own the dialog | The lease is released at once; the owning instance fires it on its own next tick |
| Invalid cron expression or timezone | Rejected at creation with `CronScheduleError` |

## Code anchors

- `core/src/octoforge_core/cron/api.py` — `CronJob`, `CronStore`, `CronWaker`, `Scheduler`,
  `compute_next_fire`, `count_missed`
- `core/src/octoforge_core/cron/scheduler.py` — the polling loop, claims, coalescing
- `core/src/octoforge_core/cron/store.py` — SQL store with the CAS claim
- `core/src/octoforge_core/cron/reporter.py` — outcome policy
- `core/src/octoforge_core/cron/tools.py` — `cron_pause`, `cron_resume`
- `core/src/octoforge_core/tasks/tools.py` — `task_create` with a schedule
- `core/tests/test_cron_scheduler.py`, `core/tests/test_cron_store.py`, `core/tests/test_cron_reporter.py`
