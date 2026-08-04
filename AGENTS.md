# AGENTS.md

> How to write code in this repository. Aimed at AI coding agents; humans are welcome to it too.

**What this file is not:** a description of the system. That lives in `docs/` — start at
`docs/README.md`, read `docs/concept.md` and `docs/architecture.md` before non-trivial work, then the
`docs/reference/` page for whatever you are touching. Do not re-describe behavior here; a second
description only rots.

**Keep this file and `CLAUDE.md` in sync.** `CLAUDE.md` is the shorter quick-start on top of it. When
conventions, structure, commands or language rules change, update both in the same change.

## The project in three lines

OctoForge is a self-hosted multi-user agent platform: skills, knowledge and callable HTTP endpoint
contracts live in the database and are found by embedding search; a dialog is an actor with durable
obligations; background work and cron run through the same machinery. Six packages, one dependency
rule — `core/` is a typed library that never imports a web framework, `server/` is the HTTP service
over it, `surfaces/` holds the interfaces, `deploy/` assembles a deployment from them.

## Repository layout

| Path | What |
|---|---|
| `core/src/octoforge_core/` | the library: domain modules (`agent/`, `dialogs/`, `instructions/`, `datasets/`, `memory/`, `context/`, `tasks/`, `cron/`, `secrets/`, `net/`, `search/`, `vision/`, `speech/`, `admin/`), framework packages (`tools/`, `llm/`, `db/`) and the builders in `composition.py` |
| `server/src/octoforge_server/` | the HTTP service: `app.py`, `api/`, `auth.py`, `deps.py`, `config.py`, `capabilities.py`, and the `Surface` port in `surfaces.py`. Imports no interface |
| `surfaces/telegram/`, `surfaces/console/`, `surfaces/webui/` | the interfaces, one package each. None imports another |
| `deploy/src/octoforge_deploy/` | the composition root `main.py:runtime()`, the HTTP entry point, and the Telegram-only one. The only package that may import every other |
| `core/tests/`, `deploy/tests/` | one module per source module, plus the boundary and modularity guards |
| `docs/` | the documentation set — read `docs/CONVENTIONS.md` before editing it |
| `tools/` | operator scripts: `quickstart.py`, `hash_password.py`, `bench_latency.py`, `check_docs.py`, `pg_backup.sh`, `sqlite_to_postgres.py` |

A domain module always has the same shape: `api.py` (its ports, DTOs and errors — the only thing
neighbours import), `models.py` (ORM rows), `store.py` (SQL), `tools.py` (its agent-facing tools).
`tools/` and `db/` are framework: they import no domain module, and tests enforce it.

## Commands

| Command | What it does |
|---|---|
| `make check` | the gate: ruff → mypy strict → `tools/check_docs.py` → pytest, both projects |
| `make lint` / `make format` / `make typecheck` / `make test` / `make docs` | the individual steps |
| `make install` | create `.venv` with both projects editable (includes the local-embeddings extra) |
| `make upgrade` | bring an existing `.venv` up to what CI resolves — use it when `make check` disagrees with CI on unchanged code |
| `make test-pg` | the Postgres-specific store tests (`make check` skips them); run after touching `db/`, a `*_store.py` or a migration |
| `make audit` | `pip-audit` over the dependency tree; CI runs it as its own step |
| `make bench` | the latency harness behind the numbers in `README.md` and `docs/guides/performance.md` |
| `make quickstart` / `quickstart-logs` / `quickstart-down` | the local docker stack from a fresh clone |
| `make run` / `make run-telegram` | uvicorn with autoreload / the bot alone, no HTTP port |
| `make db-up` / `db-down` / `db-psql` | the compose Postgres service |

One test: `cd core && ../.venv/bin/pytest tests/test_router.py -k name` — pytest and mypy config live
in each project's `pyproject.toml`, so the working directory matters.

ruff and mypy are version-capped in the dev extras on purpose: their releases change verdicts on
unchanged code. Bump a cap deliberately, with the fallout in the same change.

### Runtime reference

| What | Value |
|---|---|
| Web/API port | `8000` |
| Probes | `GET /health` (liveness), `GET /health/ready` (touches the database) |
| API docs | `GET /docs`, schema at `/openapi.json` |
| Operator console | `/admin.html`, HTTP Basic (`OF_ADMIN_USERNAME` / `OF_ADMIN_PASSWORD_HASH`; empty hash = 503) |
| Telegram | no HTTP port — long polling only |
| Deployment | `docker compose up -d` = postgres + app (HTTP, console and bot in one process) + caddy; `--profile standalone` runs the bot alone |
| Logs | stdout/stderr, plus a rotating file per process under `OF_LOG_DIR` (compose mounts `./logs`: `app.log`, `ingest.log`; 2 GB each, survives a redeploy) |
| Config | `.env`, every variable prefixed `OF_` — annotated list in `.env.example`, reference in `docs/reference/configuration.md` |

## Code conventions

1. **UTC everywhere.** Time comes from `utc_now()` (`core/src/octoforge_core/time.py`); naive
   datetimes are forbidden, and the `UTCDateTime` column type enforces it on both dialects.
2. **Full typing.** Every argument, return and attribute is annotated (ruff `ANN` + mypy strict). A
   bare `Any` in an annotation is banned; `dict[str, Any]` is fine at a JSON boundary.
3. **Objects, not dicts.** Data travels as dataclasses and `StrEnum`s; a dict is validated into an
   object at the boundary and never carried further.
4. **No magic values.** Meaningful literals are named constants, limits come from config, tests use
   `HTTPStatus`.
5. **Dependencies point inward.** `core/` never imports fastapi, external clients arrive through
   `Protocol` ports, and nothing constructs its own dependencies — the graph is assembled in the
   composition root (`deploy/src/octoforge_deploy/main.py`, builders in `composition.py`). Enforced by
   `core/tests/test_boundaries.py`.
6. **Complexity limits are enforced** (`C901` ≤ 10, `PLR0915` ≤ 50 statements, `PLR0911` ≤ 6
   returns). Split the function; do not disable the rule.
7. **Tests ship with the change.** pytest + pytest-asyncio, with the LLM and HTTP mocked.
8. **Migrations are append-only.** A `PreToolUse` hook blocks edits to any migration already in git
   HEAD — add a new one. Write them dialect-neutrally (`sa.false()` rather than `sa.text('0')`; both
   `sqlite_where=` and `postgresql_where=` on partial indexes).
9. **Language.** Commit messages, comments, docstrings, `README.md`, `docs/` and this file are
   English. Conversation with the user follows whatever language they use.
10. **Communication style.** Structured, medium detail — enough to be useful, not exhaustive.

## Workflow rules

- **Git and `gh` mutations need explicit permission every time** — commit, push, release, repo
  settings, issues. Being authenticated is not standing permission.
- **Definition of done: `make check` passes.** Run it yourself and show the output; never take a
  subagent's word for its own work — read the diff.
- **Mocked tests are not proof.** The gate mocks the LLM and HTTP, so for anything touching
  `agent/loop.py`, SSE delivery or `telegram/`, exercise it live and read the raw output.
- **No stop-the-world.** One asyncio process serves every dialog, so blocking code freezes all users.
  For every path ask: does it run on the loop, and does its cost grow with data? If it can exceed
  ~10 ms at scale, vectorize it and move it to `asyncio.to_thread` — and chunk long C calls, which
  hold the GIL even from a worker thread. Never hold a cross-dialog lock across an await, and never
  let a latency-critical action queue behind slow work. Measured cases live in
  `docs/guides/performance.md`.
- **Plan before subtle areas.** `agent/router.py`, `agent/runner.py`, `cron/` and `context/` have
  non-obvious invariants (branch reconstruction, watermarks, the pull model). A one-file, obviously
  scoped fix does not need a plan.
- **Docs ship with the code.** A behavior change edits the matching page under `docs/` in the same
  commit, following `docs/CONVENTIONS.md`; `make check` fails when a documented path or link breaks.
- **Parallelize once the plan is set.** Tests, docs and implementation can be written at the same
  time once the interfaces are fixed.
- **Get a second opinion** on a non-trivial diff — a review pass in a fresh context catches what a
  green suite does not.
- **Pick subagent capability by task:** a fast tier for mechanical search, a standard tier for
  implementation and review, a heavy tier only for hard architectural tradeoffs. Say which you used.
- **On compaction, keep** the modified files, any `OF_*` variables or migration ids touched, and the
  last `make check` result.

## Tooling

ruff (lint + format; rules `E, F, I, UP, B, SIM, ANN, C90, PL, RUF`, line length 100), mypy strict,
pytest + pytest-asyncio, `pip-audit`. The `Makefile` is the entry point — prefer it over ad-hoc
command lines. `gh` is installed and authenticated for this repository; check `gh auth status` before
assuming otherwise.
