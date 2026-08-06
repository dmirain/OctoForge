# Data model

One relational database, two dialects, one migration chain. Tables live with the module that owns them,
and no ORM object ever crosses a module boundary — stores map rows to their module's DTOs.

## Tables

| Table | Owner module | Holds |
|---|---|---|
| `users` | `identity/` | A person: opaque id (an id that can be parsed will be parsed) and their canonical `name`, seeded by the first surface that reports one |
| `user_identities` | `identity/` | What one surface calls a person; unique on `(surface, external_id)`; mirrors the surface profile (`name`, optional `username`) |
| `dialogs` | `dialogs/` | One row per `(user_id, channel)` |
| `messages` | `dialogs/` | The narrative and archive: role, content, kind, per-dialog `seq`, `exchange_id`, `task_id`, attachments |
| `exchanges` | `dialogs/` | Obligations: status, title, owning task, pending question |
| `dialog_claims` | `dialogs/` | Which process runs a dialog's actor: owner, generation, heartbeat |
| `tasks` | `tasks/` | Units of work: kind, status, input, result/error, delivery timestamps |
| `instructions` | `instructions/` | Skills, knowledge, endpoints and memories with their embeddings, ownership and authorship |
| `datasets`, `dataset_records` | `datasets/` | User datasets, their schemas and validated records |
| `dialog_summaries` | `context/` | Compacted segments: `[seq_from, seq_to]`, topics, summary text |
| `cron_jobs` | `cron/` | Schedules, next/last fire, claim fields, last status |
| `secrets` | `secrets/` | Encrypted per-user values with their host binding, required description, allowed placements and optional transform |
| `user_params` | `params/` | Plaintext per-user values endpoint templates reference as `{user.code}` (timezone, account ids); set by the operator in the console |
| `secret_form_links` | `secrets/` | Short-lived capability codes for the secrets form, with the prefill the agent put in them; swept when the next code is issued |
| `tariffs` | `tariffs/` | Operator-defined plans: feature codes plus nullable numeric caps (NULL = unlimited in that dimension); at most one row is the default plan |
| `user_tariffs` | `tariffs/` | At most one plan binding per user; no row = the default plan, or no restrictions when none is marked |
| `usage_events` | `tariffs/` | Insert-only ledger of metered actions: kind, origin, token counts, quantity and the ids of the entities the spend belongs to |

The Telegram invite store is separate: its own declarative base and its own database
(`OF_TELEGRAM_DATABASE_URL`), holding invite codes and member profiles. On Postgres that is a second
database on the same server.

## Two dialects

| | Postgres | SQLite |
|---|---|---|
| Driver | `postgresql+asyncpg://` (the `postgres` extra) | `sqlite+aiosqlite://` (bundled) |
| Role | Deployment target | Tests, embedded single-process setups |
| Writers | Many | Exactly one — so one process only |

Dialect-sensitive store behavior is covered by `core/tests/test_postgres_stores.py`, which skips unless
`OF_TEST_DATABASE_URL` is set (`make test-pg` provides it). `make check` runs the SQLite path.

## Time

Every timestamp is timezone-aware UTC, obtained through `utc_now()`. Naive datetimes are forbidden, and
the rule is enforced by the column type rather than by discipline: `UTCDateTime` is a `TypeDecorator` that

- maps to native `timestamptz` on Postgres (asyncpg rejects an aware value bound to a naive column),
- stores normalized naive values on SQLite, which has no timezone support, and re-stamps them as UTC on
  read.

The Python-side contract is therefore identical on both: aware UTC in, aware UTC out.

## Schema management

`bootstrap_schema(engine)` brings the database to the latest Alembic revision at startup; on an empty
database it creates the schema from the models and stamps it at head. `init_db(engine)` is the
`create_all` shortcut for tests and quick embedding, not for deployment.

Migrations are **one chain** in `core/src/octoforge_core/db/migrations/versions/`, deliberately: history is
global and linear, and cross-module migrations exist. Per-module chains are only for a module with its own
database — which is why the Telegram invite store has no Alembic at all and creates its schema directly.

Two rules apply to writing them:

- **Append-only.** A migration already in git `HEAD` is never edited; a new one is added instead. A
  `PreToolUse` hook in `.claude/settings.json` blocks such edits for AI agents, and the same rule applies
  to humans: the file may already have run somewhere.
- **Dialect-neutral.** `sa.false()` rather than `sa.text('0')`; both `sqlite_where=` and
  `postgresql_where=` on partial indexes. Production runs Postgres, tests run SQLite, and a migration
  must pass both.

## Ownership in the schema

Isolation is a column plus a predicate, not a layer:

- `dialogs` are keyed by `(user_id, channel)`;
- `instructions` carry `owner_id` (NULL = public) and `author_id`, with uniqueness on
  `(type, title, owner_id)` plus a partial unique index over public records;
- `datasets`, `secrets` and `cron_jobs` carry their owner and every query filters by it;
- `user_tariffs` and `usage_events` carry their user; the ledger is additionally append-only, so
  concurrent writers on different nodes never contend;
- `messages` and `exchanges` inherit isolation from their dialog.

The only cross-user reader is the operator console's read model (`admin/`), and it is read-only.

## Invariants

- **All timestamps are aware UTC**, enforced by the column type.
- **`messages.seq` is per-dialog and monotonic** — it is what the compaction boundary and archive search
  are expressed in.
- **Task rows are kept forever**, terminal states included.
- **Instruction uniqueness allows one private record per owner per title, and one public record per
  title.**
- **A store never returns an ORM object across a module boundary.**
- **`db/` imports domain models only for table registration** — enforced by `core/tests/test_boundaries.py`.
- **Migrations are append-only and dialect-neutral.**

## Configuration

| Variable | Effect |
|---|---|
| `OF_DATABASE_URL` | The main database |
| `OF_TELEGRAM_DATABASE_URL` | The invite/member database |
| `OF_TEST_DATABASE_URL` | Enables the Postgres store tests (test databases only — the fixture drops the `public` schema, and the URL must contain "test") |

## Failure modes

| Situation | Outcome |
|---|---|
| Alembic upgrade fails at startup | Logged with the traceback, and the schema falls back to `create_all` so a fresh deployment still starts. An existing database with a failed migration needs manual attention |
| SQLite with more than one process | Write contention — SQLite allows one writer |
| A naive datetime reaches a column | Normalized to UTC by the column type; the code that produced it is the bug |
| Editing a migration that already ran | Blocked by the hook for agents; for humans it silently diverges environments — add a new migration instead |
| Missing `asyncpg` with a Postgres URL | Import error at startup: install the `postgres` extra |

## Code anchors

- `core/src/octoforge_core/db/base.py` — `Base`, `UTCDateTime`
- `core/src/octoforge_core/db/engine.py` — engine, session factory, `init_db`, `bootstrap_schema`
- `core/src/octoforge_core/db/migrations/` — the single Alembic chain
- `core/src/octoforge_core/*/models.py` — the tables, per module
- `core/src/octoforge_core/time.py` — `utc_now()`
- `core/tests/test_migrations.py`, `core/tests/test_postgres_stores.py`
