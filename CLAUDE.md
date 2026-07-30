# CLAUDE.md

Quick-start for Claude Code in this repository. `AGENTS.md` is the full version — conventions,
workflow rules, tooling — and the two must stay in sync when either changes.

**The system is documented in `docs/`, not here.** Start at `docs/README.md`; read `docs/concept.md`
and `docs/architecture.md` before non-trivial work, then the `docs/reference/` page for whatever you
are touching (`docs/reference/exchanges.md` and `docs/reference/conversation-actor.md` are the core
mental model). Nothing about behavior belongs in this file.

OctoForge is a self-hosted multi-user agent platform: skills, knowledge and callable HTTP endpoint
contracts live in the database and are found by embedding search; a dialog is an actor with durable
obligations; background work and cron run through the same machinery. `core/` is a typed library that
never imports a web framework; `web/` is the FastAPI and Telegram adapter on top of it.

## Commands

| Command | What it does |
|---|---|
| `make check` | the gate: ruff → mypy strict → `tools/check_docs.py` → pytest, both projects |
| `make lint` / `make format` / `make typecheck` / `make test` / `make docs` | the individual steps |
| `make install` | create `.venv` with both projects editable |
| `make upgrade` | refresh `.venv` to what CI resolves — when `make check` disagrees with CI on unchanged code |
| `make test-pg` | Postgres store tests (skipped by `make check`); run after touching `db/`, a `*_store.py` or a migration |
| `make audit` | `pip-audit` over the dependency tree |
| `make bench` | latency harness; its numbers are quoted in `README.md` |
| `make quickstart` / `quickstart-logs` / `quickstart-down` | the local docker stack from a fresh clone |
| `make run` / `make run-telegram` | uvicorn with autoreload / the bot alone, no HTTP port |
| `make db-up` / `db-down` / `db-psql` | the compose Postgres service |

One test — note the per-project working directory:

```bash
cd core && ../.venv/bin/pytest tests/test_router.py -k test_name
cd web  && ../.venv/bin/pytest tests/test_dialog_api.py
```

Config is `.env` (see `.env.example`); every variable is prefixed `OF_`, and
`docs/reference/configuration.md` explains each one. The startup log prints which capabilities the
current configuration actually enables — read it before debugging anything that "does nothing".

## Runtime reference

Take these from here, don't guess:

| What | Value |
|---|---|
| Web/API port | `8000` |
| Probes | `GET /health`, `GET /health/ready` (touches the database) |
| API docs | `GET /docs`, schema at `/openapi.json` |
| Operator console | `/admin.html`, HTTP Basic (`OF_ADMIN_USERNAME` / `OF_ADMIN_PASSWORD_HASH`; empty hash = 503 everywhere) |
| Telegram | no HTTP port — long polling only |
| Deployment | `docker compose up -d` = postgres + app (HTTP, console and bot in one process) + caddy; `--profile standalone` runs the bot alone |
| Logs | stdout/stderr only — redirect yourself when backgrounding |

## Rules that bite

Full list in `AGENTS.md`; these are the ones that change what you do today.

- **Git and `gh` mutations need explicit permission every time** — commit, push, release, repo
  settings. Being authenticated is not standing permission.
- **Done means `make check` passes** — run it yourself, show the output, and read the diff rather
  than trusting a summary.
- **Mocked tests are not proof.** The gate mocks the LLM and HTTP; anything touching
  `agent/loop.py`, SSE delivery or `telegram/` also gets exercised live.
- **No stop-the-world.** One asyncio process serves every dialog. If a code path runs on the loop and
  its cost grows with data, it must not exceed ~10 ms at scale: vectorize, `asyncio.to_thread`, chunk
  long C calls. Measured cases: `docs/guides/performance.md`.
- **Plan first** for `agent/router.py`, `agent/runner.py`, `cron/`, `context/` — non-obvious
  invariants. A one-file obvious fix does not need it.
- **Docs ship with the code**, following `docs/CONVENTIONS.md`; `make check` fails on a broken
  documented path or link.
- **Migrations are append-only** — a hook blocks editing one that is already in git HEAD; add a new,
  dialect-neutral migration instead.
- **UTC only**, via `utc_now()`; naive datetimes are forbidden.
- **Full typing**, no bare `Any`; data travels as objects and `StrEnum`s, dicts only at the JSON
  boundary.
- **Language:** commits, comments, docstrings, `README.md` and `docs/` in English; conversation in
  whatever language the user writes.
- **Subagent models by task:** `haiku` for mechanical search, `sonnet` for implementation, tests and
  review, `opus`/`fable` only for hard architectural tradeoffs. Name the model when reporting back.
- **On compaction, keep** the modified files, any `OF_*` variables or migration ids touched, and the
  last `make check` result.
