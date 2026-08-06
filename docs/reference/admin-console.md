# Operator console

The one place that looks across users. Everywhere else a query is scoped to its owner; an operator needs the
opposite view — every dialog, every task, every record — so that view is a module of its own, and it is
read-only.

## How it works

`AdminReadModel` (`core/src/octoforge_core/admin/api.py`) is a `Protocol` of paginated listings returning
`Page[T]` (items plus the total, so the UI can page). Its SQL implementation lives next to the other stores.

Why a module rather than a few queries in the web layer: the web adapter never imports SQLAlchemy, and the
cross-user view is exactly the kind of thing that would tempt it to. Putting the read model in core keeps
that boundary intact — enforced by `core/tests/test_boundaries.py`.

### What it exposes

| Listing | Contents |
|---|---|
| `totals` | Counts per entity, for the console header |
| `list_dialogs` | Every dialog with its user, channel and activity |
| `list_messages(dialog_id)` | The full message log of one dialog |
| `list_tasks` | Tasks across dialogs, filterable |
| `list_cron_jobs` | Every schedule with its next fire and last status |
| `list_instructions` | Skills, knowledge and endpoints across owners |
| `list_datasets`, `list_dataset_records` | Datasets and their contents |
| `list_memories` | Memories in their own shape (key, owner) |
| `list_summaries` | Compaction summaries |
| `list_exchanges` | Obligations with statuses |
| `list_user_params` | Per-user params — the values endpoint templates reference as `{user.code}` |
| `list_secrets` | Secret *metadata* across users: code, host, description, placements, transform — never a value (the ciphertext column is not even selected) |
| `list_usage_events` | The usage ledger, newest first, with the dialog/exchange/task links of every event |
| `usage_report(days)` | Consumption aggregated per (user, kind, origin) over the report window |

Page size defaults to 50 and is capped at 500.

Every listing that names an owner also carries their name (`user_name` / `owner_name` /
`author_name`), resolved from the identity module in one query per page. The console renders the
person column by name with the opaque id in the tooltip; an unnamed person (no surface has reported
a name yet) falls back to the id.

`GET /api/admin/users` lists people and the accounts each one answers on. A person is the unit here,
not a handle: the same human may arrive from Telegram and from a browser, and everything they own is
filed under them. Revoked identities are listed rather than hidden — that an account was once theirs
is part of the answer to "who is this". The payload carries each person's admission status and the
installation's head-counts (`active_count`, `waiting_count`, `banned_count`, `max_active_users`) —
the «Пользователи» tab is where the operator runs the queue: ban/unban/activate buttons and the
active-user cap field (see [access.md](access.md)).

The Telegram surface adds its own listing (`GET /api/admin/telegram/users`), which joins the member
directory with invite attribution: names, usernames and which invite somebody came through are
Telegram's own knowledge, and only Telegram can answer for them.

### Mutations are not part of it

Two actions are available from the console — publishing an instruction and enabling or disabling a cron
job — plus deletions of dialogs, tasks and cron jobs, the per-user params CRUD
(`POST /api/admin/params`, `DELETE /api/admin/params/{code}?user_id=…`): the console is where an
operator sets a user's `{user.*}` values, and the write goes through the same `UserParamStore` the
call executor reads. The tariff catalog is managed here too (`POST/DELETE /api/admin/tariffs`,
`POST /api/admin/tariffs/assign` — see [tariffs.md](tariffs.md)), through the same `TariffStore` the
limit gate reads; so are user statuses (`POST /api/admin/users/{id}/status?status=…` — a ban pauses
the person's cron jobs, an unban resumes nothing) and the installation settings
(`GET/POST /api/admin/settings`, `DELETE /api/admin/settings/{key}` — see
[access.md](access.md)). All of them go through the same owner-scoped services a user action would
(`InstructionService.publish`, `CronStore.set_enabled`, `TaskStore.delete`,
`DialogRepository.delete`, …), so an admin cannot bypass an invariant that protects a user.

Deleting a dialog is a coordinated cleanup: each module removes its own rows (summaries, tasks, then
messages and the dialog itself in one transaction). Cron jobs survive it — they belong to the user, not to
the dialog, and the next firing simply creates a fresh dialog.

### The page

`/admin.html` is one static page: tables per entity, pagination, drill-downs (dialog → messages,
dataset → records), and a viewer for long fields. It sits behind the same HTTP Basic credential as the rest
of the surface, and its labels are in Russian — the deployment it was written for. It is a tool for the
operator, not a product surface.

## Invariants

- **The read model is read-only.** No listing writes anything.
- **Mutations reuse owner-scoped services**, never a cross-user write path.
- **This is the only cross-user reader.** Every other query filters by owner.
- **Memories are hidden from cross-user instruction search** unless asked for explicitly.
- **Page size is bounded** (max 500) so a console request cannot pull an entire table.
- **The web adapter still does not import SQLAlchemy** — it depends on the port.

## Configuration

| Variable | Effect |
|---|---|
| `OF_ADMIN_USERNAME`, `OF_ADMIN_PASSWORD_HASH` | The credential guarding the console (and everything else) |
| `OF_TELEGRAM_ADMIN_IDS` | Who gets the in-chat `admin_manage` tool (a different surface, same intent) |

## Failure modes

| Situation | Outcome |
|---|---|
| No credential configured | The console answers 503 like the rest of the HTTP surface |
| Publishing a memory | Reported as not found — memories are never publishable |
| Deleting a dialog with live processes | Rows are removed; the actor's in-memory state is dropped on the next restart. Prefer stopping work first |
| Very large table | Bounded pages; the total tells the operator what they are paging through |

## Code anchors

- `core/src/octoforge_core/admin/api.py` — `AdminReadModel`, `Page[T]`, the DTOs
- `core/src/octoforge_core/admin/store.py` — the SQL implementation (SELECT and count only)
- `surfaces/console/src/octoforge_console/routes.py` — the HTTP surface and the two mutations
- `surfaces/console/src/octoforge_console/static/admin.html` — the page
- `surfaces/telegram/src/octoforge_telegram/admin.py` — the in-chat admin tool
- `core/tests/test_admin_store.py`, `deploy/tests/test_admin_api.py`
