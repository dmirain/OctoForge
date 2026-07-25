# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

OctoForge is a multi-user LLM agent: tools as executable Jinja templates, knowledge stored in the DB, background tasks with notifications. Two surfaces: a web chat UI and a Telegram bot.

The living design doc is `docs/design.md`; code conventions are `AGENTS.md`. Read them before non-trivial work; this file is a shorter quick-start covering similar ground.

**Keep this file and `AGENTS.md` in sync.** They describe the same project from two angles. When one changes — conventions, structure, commands, language rules — check whether the other needs the same update in the same change. Don't let them drift apart.

## Commands

All checks and runs go through the `Makefile`:

- `make install` — create `.venv` and install both projects editable with dev deps
- `make check` — full gate: `ruff check` → `ruff format --check` → `mypy strict` → `pytest`, for both projects
- `make lint` / `make format` / `make typecheck` / `make test` — individual steps
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
| Logs | stdout/stderr only (`logging.basicConfig`, no file handler) — redirect yourself if backgrounding, e.g. `make run > /tmp/octoforge.log 2>&1 &` |

## Architecture

**Monorepo of two independent Python projects**, each with its own `pyproject.toml`, deps and tests:

- `core/` — library `octoforge-core` (src-layout). Domain, ports, services, LLM clients. **Never imports fastapi**; sqlalchemy appears only in `db/` and the SQL stores (`instructions/store.py`, `datasets/store.py`, `memory/store.py`, `context/store.py`, `cron/store.py`).
- `web/` — app `octoforge-web` (src-layout). Thin FastAPI adapter + Telegram adapter. Depends on `octoforge-core`.

Clean-architecture dependency rule: dependencies point inward. External clients (LLM, HTTP, DB) reach services through `Protocol` ports. Reusable builder functions (`build_llm_client`, `build_tool_registry`, `build_conversation_manager`, etc.) live in `core/composition.py` — ports and configs only, no fastapi; `web/src/octoforge_web/main.py:runtime()` is just the default assembly on top of them (shared by the HTTP app and the standalone Telegram surface), and an alternative composition root can reuse the same builders without copying code.

### The process model (the core mental model)

The dialog is an actor, not a request/response handler:

- `ConversationRunner` owns one dialog's **narrative** (user messages + process finals + system notifications — only this is persisted) and its **processes** (foreground/background, in-memory).
- `ConversationManager` maps `(user_id, channel)` → runner (get-or-create). Isolation is by that pair.
- `AgentLoop.stream(history, control, context) -> AsyncIterator[LoopEvent]` is the loop as an event stream: tokens, tool calls, final, cancellation. `LoopControl` is a cancellation flag (the pull model replaced message injection: branches re-sync from the narrative at every iteration).
- **LLM router** (`agent/router.py`): a one-shot `route(ops)` tool classifies each incoming message into ops (INJECT / START_NEW / CANCEL). A deterministic guardrail strips START_NEW from any batch that also has INJECT (an injection must not spin the question into the background). Process count is capped by `OF_MAX_PROCESSES`.
- Background tasks are background processes: `task_create` / `task_list` / `task_delete` tools. Every process is backed by a task row (ANSWER or RUN); rows are kept forever (terminal states included), and result delivery is tracked by `delivered_at`.

### Tools

`Tool` / `ToolSpec` / `ToolContext` / `ToolRegistry` (no origin kinds — `SkillOrigin` was removed). The `tools/` package is framework only; tool implementations live in their domain modules (`cron/tools.py`, `memory/tools.py`, `datasets/tools.py`, `context/tools.py`, `tasks/tools.py`, `search/tools.py`, `net/tools.py`, `instructions/tools.py`) and are registered in the composition root. Notable ones: `http_request`, `external_call` (over DB endpoint-records, behind the `SsrfGuard`), `instruction_search` / `instruction_save`, `data_put` / `data_query` / `data_forget`, `memory_store` / `memory_search` / `memory_delete`, `task_create` / `task_list` / `task_delete` (one surface for background tasks and cron jobs — `task_create` with a `schedule` creates the cron job), `cron_pause` / `cron_resume`, `web_search` (serper.dev, only when `OF_SERPER_TOKEN` is set).

### Self-contained domain modules

Each is a package with an `api.py` boundary (a `Protocol` + DTOs) and a local SQL-backed implementation:

- `instructions/` — knowledge/skill/endpoint records in the `instructions` table; cosine ranking + exact-title boost + optional cross-encoder rerank of the shortlist. The system-owned slice (`system` flag) is a declarative registry (`CORE_SYSTEM_SKILLS` in core, `WEB_SYSTEM_SKILLS` in web) synced at startup; agent-facing save/delete refuse system records.
- `datasets/` — user data (`datasets` / `dataset_records`), JSON-schema validation, owner isolation at the SQL level; descriptors also feed `instruction_search`.
- `memory/` — key/value memories, `user_id` NULL = global scope, LIKE search over "own + global".
- `cron/` — `CronScheduler` asyncio loop with CAS lease (`lease_ttl`), coalescing missed fires; a fire calls `ConversationManager.wake` → a background process. See `docs/cron.md`.
- `context/` — dialog narrative compaction: a rolling summary (`dialog_summaries` table, via `LlmContextCompactor`) plus a verbatim hot tail, triggered by char/token thresholds or reactively on `ContextOverflowError`. See `docs/context.md`.

### Embeddings / reranker (optional but needed for instructions & datasets)

`EmbeddingClient` port has two backends chosen by `OF_EMBEDDING_BACKEND`: local sentence-transformers (`llm/local_embeddings.py`) or an OpenAI-compatible HTTP endpoint (`llm/embeddings.py`). Optional `RerankerClient` also has two backends: a local cross-encoder (`llm/reranker.py`) or an HTTP one (`llm/http_reranker.py`, SiliconFlow-compatible, gated on `OF_RERANKER_API_KEY`). Without a working embedding backend the app still starts (the registry sync is skipped), but instruction/dataset search & save are unavailable.

`sentence-transformers` (and torch) is the optional `local-embeddings` extra on `octoforge-core` — not a hard dependency. Importing `octoforge_core` never requires it; only constructing `SentenceTransformerEmbedder`/`CrossEncoderReranker` does, and each raises a clear `ImportError` with the install command if it's missing. `web` depends on `octoforge-core[local-embeddings]`, so `make install`/`make run` get it by default; a pure-library consumer that only wants the OpenAI-compatible backends can skip it entirely and avoid the torch download.

### Telegram surface

`web/src/octoforge_web/telegram/` — raw-httpx Bot API client (no aiogram), `TelegramPoller` (long-poll), `TelegramBridge` renders runner events into a throttled draft message. Channel `"telegram"`, `user_id = "tg:<id>"`, private chats only. Agent markdown answers are converted to Telegram HTML (`markdown.py`) with a plain-text fallback; a final containing a table/checklist/`<details>`/math is upgraded in place to a Bot API 10.1 Rich Message (`telegram/rich.py`, toggle `OF_TELEGRAM_RICH_MESSAGES`, ≤ 32,768 chars, one message only — falls back to the HTML version on failure). Access is gated by an invite system (`telegram/invites/`, its own SQLite DB via `OF_TELEGRAM_DATABASE_URL`): admins (`OF_TELEGRAM_ADMIN_IDS`) always pass, everyone else needs `/start <code>` with a code that hasn't expired (`OF_TELEGRAM_INVITE_TTL_SECONDS`); the gate only activates once the admin list is non-empty. An `admin_manage` tool (list/generate/revoke/restore invites, cross-user instruction search/publish) is hidden from non-admins via the same `visible_to(context)` hook `ToolRegistry` uses elsewhere. Runs alongside web, or standalone (`python -m octoforge_web.telegram`) with no HTTP listener.

## Conventions worth flagging (full list in AGENTS.md)

- **UTC everywhere** — timezone-aware UTC only, obtained via `utc_now()` (`octoforge_core/time.py`); naive datetimes are forbidden (a SQLite `TypeDecorator` enforces it).
- **Full typing** (ruff `ANN` + mypy strict); bare `Any` in annotations is banned (ANN401). Data travels as domain objects/enums (`StrEnum`), not dicts — dicts only at the JSON boundary.
- **Complexity limits** are enforced (`C901` ≤ 10, `PLR0915` ≤ 50 statements, `PLR0911` ≤ 6 returns): split functions, don't disable the rule.
- **Tests ship with the change** (pytest + pytest-asyncio; mock LLM/HTTP).
- **Language rule**: commit messages, docstrings and code comments are **English**. `README.md` (and anything it links to) and `AGENTS.md` are **English** too — the former is the project's public storefront, the latter is AI coding-agent guidance, conventionally written in English. Everything else — conversation, `docs/`, any other documentation — follows whatever language the user asks for; don't default to a fixed one.
- **Communication style**: structure responses clearly and keep the level of detail medium — enough to be useful, not exhaustive — always in whatever language the user is using.
- **Docs update with code**: any logic change is also written into `docs/design.md` in the same change.
- **Git mutations only with explicit permission** — ask before every `commit`/`push`/etc. Same for `gh`: check `gh auth status` before assuming it's unavailable (it may be authenticated with a repo-scoped fine-grained token) — use it for releases/issues/PRs/`gh repo edit`, but being authenticated isn't standing permission, ask before consequential actions.
- **Migrations are append-only**: a `PreToolUse` hook (`.claude/settings.json`) blocks edits to any Alembic migration file already committed to git HEAD. Add a new migration file instead of editing an old one.
- **Plan before touching subtle areas**: changes to `agent/router.py`, `agent/runner.py`, `cron/`, or `context/` (compaction) have non-obvious invariants (branch reconstruction, watermarks, the pull model) — use plan mode first. A one-file, obviously-scoped fix doesn't need it.
- **Parallelize once the plan is set**: tests and the matching `docs/design.md` update don't have to wait for the implementation to land. Once a plan fixes the interfaces and behavior, write (or delegate to parallel subagents) the tests, the docs update, and the implementation at the same time instead of serializing them.
- **Definition of done**: a change isn't done until `make check` passes — run it yourself and show the output, don't just assert success, and don't take a subagent's summary of its own work as confirmation — look at the actual diff or output.
- **Mocked tests aren't proof of behavior**: `make check` mocks the LLM and HTTP (rule above) — a green run doesn't prove the agent loop, SSE stream, or Telegram bridge actually works end to end. For changes touching `agent/loop.py`, SSE delivery, or `telegram/`, also exercise it live (`make run` plus a real message, or the `/verify` skill) and look at the raw output, not just an assertion that it should work.
- **Get a second opinion before shipping**: for a non-trivial diff, run a review pass (e.g. `/code-review`) in a fresh context in addition to `make check` — a green test suite doesn't catch every logic gap.
- **Choose subagent models by task, don't default**: `haiku` for mechanical grep/exploration on a known pattern; `sonnet` for implementation, tests, review, and most exploration; `opus`/`fable` only for genuinely hard architectural tradeoffs or arbitrating conflicting reviews. Name the model in the subagent's description and note it when reporting back.
- **On compaction, keep the essentials**: when a long session gets compacted, always preserve the list of modified files, any `OF_*` env vars or migration ids touched, and the last `make check` result.
