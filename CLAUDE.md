# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

OctoForge is a multi-user LLM agent: tools as executable Jinja templates, knowledge stored in the DB, background tasks with notifications. Two surfaces: a web chat UI and a Telegram bot.

The living design doc is `docs/design.md`; code conventions are `AGENTS.md`. Read them before non-trivial work; this file is a shorter quick-start covering similar ground.

**Keep this file and `AGENTS.md` in sync.** They describe the same project from two angles. When one changes — conventions, structure, commands, language rules — check whether the other needs the same update in the same change. Don't let them drift apart.

## Commands

All checks and runs go through the `Makefile`:

- `make install` — create `.venv` and install both projects editable with dev deps
- `make upgrade` — refresh an existing `.venv` to what CI installs; run it when `make check` disagrees with CI on unchanged code (`install` leaves already-satisfied deps alone, so the linter silently drifts). both checkers are capped in the dev extras (`ruff>=0.16,<0.17`, `mypy>=2.3,<3`) precisely because their releases change verdicts on unchanged code
- `make check` — full gate: `ruff check` → `ruff format --check` → `mypy strict` → `pytest`, for both projects
- `make lint` / `make format` / `make typecheck` / `make test` — individual steps
- `make db-up` / `make db-down` / `make db-psql` — the compose Postgres service (`postgres:18-alpine`, published on `127.0.0.1:5432`; the init script creates `octoforge_telegram`, `octoforge_test` and `octoforge_dev` next to `octoforge`)
- `make test-pg` — the Postgres store tests against that service (`make check` skips them); run it after touching `db/`, any `*_store.py` or a migration
- `make run` — uvicorn with autoreload (needs `.env` with `OF_LLM_API_KEY`); serves chat UI at http://127.0.0.1:8000
- `make run-telegram` — Telegram bot only, no HTTP listener (needs `OF_TELEGRAM_BOT_TOKEN`)

Run a single test (note the per-project working dir — pytest/mypy config lives in each `pyproject.toml`):

```bash
cd core && ../.venv/bin/pytest tests/test_router.py -k test_name
cd web  && ../.venv/bin/pytest tests/test_dialog_api.py
```

Config is `.env` (see `.env.example`); all vars are prefixed `OF_`.

### Runtime reference (for agents/scripts)

Take these from here, don't guess:

| What | Value |
|---|---|
| Web/API port | `8000` (`make run`, uvicorn) |
| Liveness | `GET /health` |
| Readiness (checks the DB) | `GET /health/ready` |
| API docs | `GET /docs` (Swagger UI), schema at `/openapi.json` — FastAPI defaults, not overridden |
| Telegram bot | no HTTP port — long-polling only (`make run-telegram`) |
| Deployment | `docker compose up -d` = postgres + app (HTTP + console + bot in one process) + caddy (TLS for `SITE_DOMAIN`); `--profile standalone` runs the bot alone. See `docs/deploy.md` |
| Operator console | `https://<SITE_DOMAIN>/admin.html`, HTTP Basic (`OF_ADMIN_USERNAME` / `OF_ADMIN_PASSWORD_HASH`, generate with `tools/hash_password.py`) |
| Logs | stdout/stderr only (`logging.basicConfig`, no file handler) — redirect yourself if backgrounding, e.g. `make run > /tmp/octoforge.log 2>&1 &` |

## Architecture

**Monorepo of two independent Python projects**, each with its own `pyproject.toml`, deps and tests:

- `core/` — library `octoforge-core` (src-layout). Domain, ports, services, LLM clients. **Never imports fastapi**; sqlalchemy appears only in `db/` (framework: Base/engine/migrations) and the module SQL stores (`dialogs/store.py`, `tasks/store.py`, `instructions/store.py`, `datasets/store.py`, `context/store.py`, `cron/store.py`, `secrets/store.py`). Import boundaries are test-enforced (`core/tests/test_boundaries.py`): modules talk to neighbours through `api.py` only; `db/` and `tools/` are framework and import no domain module.
- `web/` — app `octoforge-web` (src-layout). Thin FastAPI adapter + Telegram adapter. Depends on `octoforge-core`.

Clean-architecture dependency rule: dependencies point inward. External clients (LLM, HTTP, DB) reach services through `Protocol` ports. Reusable builder functions (`build_llm_client`, `build_tool_registry`, `build_conversation_manager`, etc.) live in `core/composition.py` — ports and configs only, no fastapi; `web/src/octoforge_web/main.py:runtime()` is just the default assembly on top of them (shared by the HTTP app and the standalone Telegram surface), and an alternative composition root can reuse the same builders without copying code.

### The exchange model (the core mental model)

The dialog is an actor, not a request/response handler. The authoritative doc is `docs/exchanges.md`:

- An **exchange** is a durable obligation to the user (their question, its clarifications, the final answer): a row in `exchanges` + `messages.exchange_id`, statuses OPEN → IN_PROGRESS → ANSWERED / AWAITING_USER / CANCELLED / FAILED. Exchange ≠ task: a run can finish DONE while its exchange stays open (it asked the user something via the `ask_user` tool, which parks the exchange AWAITING_USER; the user's reply resumes it with a fresh run, and an event-driven nudge re-asks after 5 min of silence).
- `ConversationRunner` owns one dialog's **narrative** (user messages + run finals + broker notes — only this is persisted) and its **processes** (in-memory). There is no foreground: every answer run streams concurrently, each event tagged with its `exchange_id`, so transports keep one draft/bubble per exchange (Telegram replies to the exchange's question). `cancel()` stops all answer runs; RUN tasks keep going.
- `ConversationManager` maps `(user_id, channel)` → runner (get-or-create). Isolation is by that pair.
- `AgentLoop.stream(history, control, context) -> AsyncIterator[LoopEvent]` is the loop as an event stream: tokens, tool calls, final, cancellation. `LoopControl` is a cancellation flag (the pull model replaced message injection: branches re-sync from the narrative at every iteration). Branch roles derive from exchange state (`agent/branch.py`): the run's own question is marked as its task, later same-exchange messages as clarifications, and questions of other live exchanges are dropped from the branch entirely.
- **LLM router** (`agent/router.py`): decides *whose* each incoming message is — NEW (its own exchange) / CONTINUE (an existing one) / COMMAND, plus `cancel_ids` — over the live exchanges; an explicit transport reply resolves deterministically (`reply_to_exchange_id`, no LLM), and any doubt or failure defaults to NEW. Exchange count is capped by `OF_MAX_PROCESSES`.
- Background tasks are background processes: `task_create` / `task_list` / `task_delete` tools. Every process is backed by a task row (ANSWER — owes an exchange an answer, or RUN); rows are kept forever (terminal states included). Notices and RUN/cron results deliver immediately; the outbox exists only to retain results while no subscriber is attached (`delivered_at` tracks delivery).

### Tools

`Tool` / `ToolSpec` / `ToolContext` / `ToolRegistry` (no origin kinds — `SkillOrigin` was removed). The `tools/` package is framework only; tool implementations live in their domain modules (`cron/tools.py`, `memory/tools.py`, `datasets/tools.py`, `context/tools.py`, `tasks/tools.py`, `search/tools.py`, `net/tools.py`, `instructions/tools.py`) and are registered in the composition root. Notable ones: `http_request`, `endpoint_get` (late binding: resolves a named endpoint's contract before the call), `external_call` (over DB endpoint-records, behind the `SsrfGuard`; a param-validation error carries the declared contract so a blind call self-corrects in one step), `recall` / `instruction_save`, `data_put` / `data_query` / `data_forget`, `memory_store` / `memory_delete` (writes into the instruction store, type=memory; no separate memory_search — `recall` covers memories), `task_create` / `task_list` / `task_delete` (one surface for background tasks and cron jobs — `task_create` with a `schedule` creates the cron job), `cron_pause` / `cron_resume`, `web_search` (serper.dev, only when `OF_SERPER_TOKEN` is set).

Tool descriptions carry policy, not just mechanics: they are the only guidance always in context (system skills arrive by search), so `recall` presents itself as the first call for any non-trivial request (one search over skills, knowledge, endpoints, dataset descriptors and the user's memories), and `http_request` / `web_search` explicitly demote themselves ("search for a stored endpoint first", "public facts only, your own knowledge lives in the store"). The system prompt's rules 1–3 say the same thing. Keep the two in sync — a measured behavior fix, not decoration (see the «Системный промпт» section in `docs/design.md`).

### Self-contained domain modules

- `secrets/` — per-user secrets: Fernet-encrypted, host-bound, resolved only inside `external_call` (endpoint records declare `auth: {secret: code}`); ingestion via `/secrets` in Telegram → one-time link → `/secrets.html` (token-authenticated, outside the Basic gate). The LLM only ever sees secret codes; responses are scrubbed of value echoes.

Each is a package with an `api.py` boundary (a `Protocol` + DTOs) and a local SQL-backed implementation:

- **system-skill overlay**: the registry in code is the default; `OF_SYSTEM_SKILLS_SOURCE=file:...` (JSON, `web/skill_overlay.py`) appends to / replaces / adds records before the startup sync — that is how this deployment adds Russian trigger phrases (`docker/system_skills.ru.json`) without rebuilding or editing core.
- `instructions/` — knowledge/skill/endpoint records in the `instructions` table; cosine ranking + exact-title boost + optional cross-encoder rerank of the shortlist. The system-owned slice (`system` flag) is a declarative registry (`CORE_SYSTEM_SKILLS` in core, `WEB_SYSTEM_SKILLS` in web) synced at startup; agent-facing save/delete refuse system records.
- `datasets/` — user data (`datasets` / `dataset_records`), JSON-schema validation, owner isolation at the SQL level; descriptors also feed `recall`.
- `memory/` — thin module: memories are `InstructionType.MEMORY` records in the instruction store (title = key, owner = user; never publishable, hidden from admin `search_all` unless asked explicitly). Tools `memory_store`/`memory_delete` only; reading goes through `recall` (embeddings, `type=memory` filter). Saves embed leniently (backend down → empty vector, startup `reembed_missing()` sweep finishes); migration `f2a6c8d1e935` folded the old `memories` table in.
- `cron/` — `CronScheduler` asyncio loop with CAS lease (`lease_ttl`), coalescing missed fires; a fire calls `ConversationManager.wake` → a background process. See `docs/cron.md`.
- `admin/` — the cross-user read model behind the operator console: `AdminReadModel` Protocol plus paginated `Page[T]` listings for every entity, read-only (mutations go through the owner-scoped services). Everywhere else queries are owner-scoped; this module is the one deliberate exception, and it lives in core so the web adapter never needs SQLAlchemy.
- `context/` — dialog narrative compaction: a rolling summary (`dialog_summaries` table, via `LlmContextCompactor`) plus a verbatim hot tail, triggered by char/token thresholds or reactively on `ContextOverflowError`. See `docs/context.md`.

### Embeddings / reranker (optional but needed for instructions & datasets)

`EmbeddingClient` port has two backends chosen by `OF_EMBEDDING_BACKEND`: local sentence-transformers (`llm/local_embeddings.py`) or an OpenAI-compatible HTTP endpoint (`llm/embeddings.py`). Optional `RerankerClient` also has two backends: a local cross-encoder (`llm/reranker.py`) or an HTTP one (`llm/http_reranker.py`, SiliconFlow-compatible, gated on `OF_RERANKER_API_KEY`). Without a working embedding backend the app still starts (the registry sync is skipped), but instruction/dataset search & save are unavailable.

`sentence-transformers` (and torch) is the optional `local-embeddings` extra on `octoforge-core` — not a hard dependency. Importing `octoforge_core` never requires it; only constructing `SentenceTransformerEmbedder`/`CrossEncoderReranker` does, and each raises a clear `ImportError` with the install command if it's missing. `web` depends on `octoforge-core[local-embeddings]`, so `make install`/`make run` get it by default; a pure-library consumer that only wants the OpenAI-compatible backends can skip it entirely and avoid the torch download.

### Telegram surface

`web/src/octoforge_web/telegram/` — raw-httpx Bot API client (no aiogram), `TelegramPoller` (long-poll), `TelegramBridge` renders runner events into one throttled draft message per exchange (each answer replies to its question; user replies resolve back to their exchange without the LLM router). Channel `"telegram"`, `user_id = "tg:<id>"`, private chats only. Agent markdown answers are converted to Telegram HTML (`markdown.py`) with a plain-text fallback; a final containing a table/checklist/`<details>`/math is upgraded in place to a Bot API 10.1 Rich Message (`telegram/rich.py`, toggle `OF_TELEGRAM_RICH_MESSAGES`, ≤ 32,768 chars, one message only — falls back to the HTML version on failure). Access is gated by an invite system (`telegram/invites/`, its own SQLite DB via `OF_TELEGRAM_DATABASE_URL`): admins (`OF_TELEGRAM_ADMIN_IDS`) always pass, everyone else needs `/start <code>` with a code that hasn't expired (`OF_TELEGRAM_INVITE_TTL_SECONDS`); the gate only activates once the admin list is non-empty. An `admin_manage` tool (list/generate/revoke/restore invites, cross-user instruction search/publish) is hidden from non-admins via the same `visible_to(context)` hook `ToolRegistry` uses elsewhere. Runs alongside web, or standalone (`python -m octoforge_web.telegram`) with no HTTP listener.

### Authentication

Every HTTP endpoint except `/health` and `/health/ready` sits behind one operator credential
(HTTP Basic, middleware in `create_app`; `web/auth.py` hashes with stdlib PBKDF2 in the
`pbkdf2_sha256:iterations:salt:digest` format — `:` because docker compose interpolates `$` in
`.env`). An empty hash fails closed with 503. This authenticates the *operator*, not the agent's
users: `X-User-Id` still selects the dialog and is still a trusted string.

## Conventions worth flagging (full list in AGENTS.md)

- **UTC everywhere** — timezone-aware UTC only, obtained via `utc_now()` (`octoforge_core/time.py`); naive datetimes are forbidden (the `UTCDateTime` `TypeDecorator` enforces it — `timestamptz` on Postgres, normalized naive on SQLite).
- **Two SQL dialects** — prod runs Postgres (`postgresql+asyncpg://`, the `postgres` extra on `octoforge-core`), tests and the embeddable setup run SQLite. Dialect-sensitive store behavior is covered by `core/tests/test_postgres_stores.py`, which skips unless `OF_TEST_DATABASE_URL` is set.
- **Full typing** (ruff `ANN` + mypy strict); bare `Any` in annotations is banned (ANN401). Data travels as domain objects/enums (`StrEnum`), not dicts — dicts only at the JSON boundary.
- **Complexity limits** are enforced (`C901` ≤ 10, `PLR0915` ≤ 50 statements, `PLR0911` ≤ 6 returns): split functions, don't disable the rule.
- **Tests ship with the change** (pytest + pytest-asyncio; mock LLM/HTTP).
- **Language rule**: commit messages, docstrings and code comments are **English**. `README.md` (and anything it links to) and `AGENTS.md` are **English** too — the former is the project's public storefront, the latter is AI coding-agent guidance, conventionally written in English. Everything else — conversation, `docs/`, any other documentation — follows whatever language the user asks for; don't default to a fixed one.
- **Communication style**: structure responses clearly and keep the level of detail medium — enough to be useful, not exhaustive — always in whatever language the user is using.
- **Docs update with code**: any logic change is also written into `docs/design.md` in the same change.
- **Git mutations only with explicit permission** — ask before every `commit`/`push`/etc. Same for `gh`: check `gh auth status` before assuming it's unavailable (it may be authenticated with a repo-scoped fine-grained token) — use it for releases/issues/PRs/`gh repo edit`, but being authenticated isn't standing permission, ask before consequential actions.
- **Migrations are append-only**: a `PreToolUse` hook (`.claude/settings.json`) blocks edits to any Alembic migration file already committed to git HEAD. Add a new migration file instead of editing an old one. Write new ones dialect-neutrally (`sa.false()` not `sa.text('0')`; both `sqlite_where=` and `postgresql_where=` on partial indexes) — prod runs Postgres, tests run SQLite.
- **Plan before touching subtle areas**: changes to `agent/router.py`, `agent/runner.py`, `cron/`, or `context/` (compaction) have non-obvious invariants (branch reconstruction, watermarks, the pull model) — use plan mode first. A one-file, obviously-scoped fix doesn't need it.
- **No stop-the-world — check on every task**: one asyncio process serves every dialog, so inline CPU work or a cross-dialog lock held over awaits freezes ALL users. If a code path's cost grows with data (records, history, users) and can exceed ~10 ms at target scale — vectorize it and/or `asyncio.to_thread` it, and mind the GIL: a single long C call over Python objects (like `np.asarray` on tuples) stalls the loop even from a worker thread — chunk it (see `instructions/ranking.py`). Latency-critical actions (cancel) must not queue behind slow commands. Full checklist in `AGENTS.md`; measured cases in the 2026-07-26 audit block of `docs/design.md`.
- **Parallelize once the plan is set**: tests and the matching `docs/design.md` update don't have to wait for the implementation to land. Once a plan fixes the interfaces and behavior, write (or delegate to parallel subagents) the tests, the docs update, and the implementation at the same time instead of serializing them.
- **Definition of done**: a change isn't done until `make check` passes — run it yourself and show the output, don't just assert success, and don't take a subagent's summary of its own work as confirmation — look at the actual diff or output.
- **Mocked tests aren't proof of behavior**: `make check` mocks the LLM and HTTP (rule above) — a green run doesn't prove the agent loop, SSE stream, or Telegram bridge actually works end to end. For changes touching `agent/loop.py`, SSE delivery, or `telegram/`, also exercise it live (`make run` plus a real message, or the `/verify` skill) and look at the raw output, not just an assertion that it should work.
- **Get a second opinion before shipping**: for a non-trivial diff, run a review pass (e.g. `/code-review`) in a fresh context in addition to `make check` — a green test suite doesn't catch every logic gap.
- **Choose subagent models by task, don't default**: `haiku` for mechanical grep/exploration on a known pattern; `sonnet` for implementation, tests, review, and most exploration; `opus`/`fable` only for genuinely hard architectural tradeoffs or arbitrating conflicting reviews. Name the model in the subagent's description and note it when reporting back.
- **On compaction, keep the essentials**: when a long session gets compacted, always preserve the list of modified files, any `OF_*` env vars or migration ids touched, and the last `make check` result.
