# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

OctoForge is a multi-user LLM agent: skills as executable Jinja templates, knowledge stored in the DB, background tasks with notifications. Two surfaces: a web chat UI and a Telegram bot.

The living design doc is `docs/design.md` and code conventions are `AGENTS.md` — **both in Russian**. Read them before non-trivial work; this file is the English quick-start on top of them.

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

## Architecture

**Monorepo of two independent Python projects**, each with its own `pyproject.toml`, deps and tests:

- `core/` — library `octoforge-core` (src-layout). Domain, ports, services, LLM clients. **Never imports fastapi**; sqlalchemy appears only in `db/` and the SQL stores (`instructions/store.py`, `datasets/store.py`, `memory/store.py`, `cron/store.py`).
- `web/` — app `octoforge-web` (src-layout). Thin FastAPI adapter + Telegram adapter. Depends on `octoforge-core`.

Clean-architecture dependency rule: dependencies point inward. External clients (LLM, HTTP, DB) reach services through `Protocol` ports; the whole object graph is assembled in one place — `web/src/octoforge_web/main.py:runtime()` (the composition root, shared by the HTTP app and the standalone Telegram surface).

### The process model (the core mental model)

The dialog is an actor, not a request/response handler:

- `ConversationRunner` owns one dialog's **narrative** (user messages + process finals + system notifications — only this is persisted) and its **processes** (foreground/background, in-memory).
- `ConversationManager` maps `(user_id, channel)` → runner (get-or-create). Isolation is by that pair.
- `AgentLoop.stream(history, control, context) -> AsyncIterator[LoopEvent]` is the loop as an event stream: tokens, skill calls, final, cancellation. `LoopControl` carries message injections + cancellation that preserves the partial answer.
- **LLM router** (`agent/router.py`): a one-shot `route(ops)` tool classifies each incoming message into ops (INJECT / START_NEW / CANCEL / PROMOTE). A deterministic guardrail strips START_NEW from any batch that also has INJECT (an injection must not spin the question into the background). Process count is capped by `OF_MAX_PROCESSES`.
- Background tasks are background processes: `task_spawn` / `task_list` skills; completion notifies the narrative and marks `result_delivered`.

### Skills

`Skill` / `SkillSpec` / `SkillContext` / `SkillRegistry` (no origin kinds — `SkillOrigin` was removed). The `skills/` package is framework only; tool implementations live in their domain modules (`cron/tools.py`, `memory/tools.py`, `datasets/tools.py`, `context/tools.py`, `tasks/tools.py`, `search/tools.py`, `net/tools.py`, `instructions/tools.py`) and are registered in the composition root. Notable ones: `http_request`, `external_call` (over DB endpoint-records, behind the `SsrfGuard`), `skills_search` / `instruction_save`, `data_put` / `data_query` / `data_forget`, `memory_store` / `memory_search` / `memory_delete`, `cron_create` / `cron_list` / `cron_delete` / `cron_pause` / `cron_resume`, `web_search` (serper.dev, only when `OF_SERPER_TOKEN` is set).

### Self-contained domain modules

Each is a package with an `api.py` boundary (a `Protocol` + DTOs) and a local SQL-backed implementation:

- `instructions/` — knowledge/skill/endpoint records in the `instructions` table; cosine ranking + exact-title boost + optional cross-encoder rerank of the shortlist. The system-owned slice (`system` flag) is a declarative registry (`CORE_SYSTEM_SKILLS` in core, `WEB_SYSTEM_SKILLS` in web) synced at startup; agent-facing save/delete refuse system records.
- `datasets/` — user data (`datasets` / `dataset_records`), JSON-schema validation, owner isolation at the SQL level; descriptors also feed `skills_search`.
- `memory/` — key/value memories, `user_id` NULL = global scope, LIKE search over "own + global".
- `cron/` — `CronScheduler` asyncio loop with CAS lease (`lease_ttl`), coalescing missed fires; a fire calls `ConversationManager.wake` → a background process. See `docs/cron.md`.

### Embeddings / reranker (optional but needed for instructions & datasets)

`EmbeddingClient` port has two backends chosen by `OF_EMBEDDING_BACKEND`: local sentence-transformers (`llm/local_embeddings.py`) or an OpenAI-compatible HTTP endpoint (`llm/embeddings.py`). Optional cross-encoder `RerankerClient` (`llm/reranker.py`). Without a working backend the app still starts (the registry sync is skipped), but instruction/dataset search & save are unavailable.

### Telegram surface

`web/src/octoforge_web/telegram/` — raw-httpx Bot API client (no aiogram), `TelegramPoller` (long-poll), `TelegramBridge` renders runner events into a throttled draft message. Channel `"telegram"`, `user_id = "tg:<id>"`, private chats only. Agent markdown answers are converted to Telegram HTML (`markdown.py`) with a plain-text fallback. Runs alongside web, or standalone (`python -m octoforge_web.telegram`) with no HTTP listener.

## Conventions worth flagging (full list in AGENTS.md)

- **UTC everywhere** — timezone-aware UTC only, obtained via `utc_now()` (`octoforge_core/time.py`); naive datetimes are forbidden (a SQLite `TypeDecorator` enforces it).
- **Full typing** (ruff `ANN` + mypy strict); bare `Any` in annotations is banned (ANN401). Data travels as domain objects/enums (`StrEnum`), not dicts — dicts only at the JSON boundary.
- **Complexity limits** are enforced (`C901` ≤ 10, `PLR0915` ≤ 50 statements, `PLR0911` ≤ 6 returns): split functions, don't disable the rule.
- **Tests ship with the change** (pytest + pytest-asyncio; mock LLM/HTTP).
- **Language rule**: user-facing text and all docs (`docs/`, `README.md`, `AGENTS.md`) are **Russian**; commit messages, docstrings and code comments are **English**.
- **Docs update with code**: any logic change is also written into `docs/design.md` in the same change.
- **Git mutations only with explicit permission** — ask before every `commit`/`push`/etc.
