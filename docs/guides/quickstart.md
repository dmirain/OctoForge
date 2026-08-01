# Quickstart

From a clone to a running agent. Two paths: containers (fastest, closest to a deployment) and a local
virtualenv (what you want while changing code).

## What you need either way

An OpenAI-compatible LLM endpoint and its key. That is one credential: embeddings inherit it unless you
say otherwise, so `recall` works without a second provider. A local Ollama qualifies.

## Containers

```bash
git clone https://github.com/dmirain/OctoForge && cd OctoForge
make quickstart
```

`make quickstart` does two things:

1. Runs `tools/quickstart.py`, which writes `.env` — a generated operator password (printed once; only its
   hash is stored), a Fernet master key for the secret store, and the LLM endpoint it asks you for. An
   existing `.env` is checked, never overwritten; if something essential is missing it says so and stops.
2. Brings up Postgres and the app with the local overlay:
   `docker compose -f docker-compose.yml -f docker-compose.local.yml up -d --wait`.

Then:

| URL | What |
|---|---|
| `http://127.0.0.1:8000/` | The chat UI. The name field picks which dialog you are in |
| `http://127.0.0.1:8000/admin.html` | The operator console |
| `http://127.0.0.1:8000/docs` | The API |

Log in with the printed credential. `make quickstart-logs` follows the app; `make quickstart-down` stops it.

The overlay is built to be harmless next to a real deployment from the same checkout: its own compose
project name and Postgres volume, its own image tag, no published database port, no Caddy, and the Telegram
bot stays off unless you set `OF_QUICKSTART_TELEGRAM_TOKEN` (a bot can only be polled by one process — a
second poller would steal a live bot's updates). The image is built without torch, so the first build takes
minutes rather than tens of minutes.

## Local virtualenv

```bash
make install            # .venv with both projects editable, including local embeddings
cp .env.example .env    # fill in OF_LLM_API_KEY at minimum
make run                # uvicorn with autoreload on http://127.0.0.1:8000
```

Two things to know:

- `OF_ADMIN_PASSWORD_HASH` must be set or **every HTTP request answers 503** — it fails closed. Generate one
  with `python tools/hash_password.py`, or let `tools/quickstart.py` write the whole file.
- `make install` includes the `local-embeddings` extra (sentence-transformers, torch). If you only use HTTP
  embeddings you can skip it: `pip install -e "core[dev]" -e "web[dev]"`.

The default database is SQLite in the working directory, which is fine for one process. For Postgres,
`make db-up` starts the compose service and its init script creates `octoforge_dev` next to the main
database — point `OF_DATABASE_URL` at the dev one, never at a deployment's.

## Check what is actually on

The first log block after startup is the capability report:

```
capabilities of this installation:
  llm                  on   gpt-4o-mini at api.openai.com
  embeddings           on   text-embedding-3-small at api.openai.com (inherited from OF_LLM_*)
  reranker             off  OF_RERANKER_MODEL is empty — recall ranks by cosine only
  ...
```

Read it before debugging anything that "does nothing": an optional capability with no configuration is off
by design, and this is where it says so. Two gaps also emit warnings, because they make an installation
useless or unreachable: missing embeddings and a missing operator credential.

## Adding Telegram

Create a bot with @BotFather, then:

```bash
OF_TELEGRAM_BOT_TOKEN=123456:ABC-...   # in .env (quickstart stack: OF_QUICKSTART_TELEGRAM_TOKEN)
OF_TELEGRAM_ADMIN_IDS=<your numeric id>
OF_TELEGRAM_BOT_USERNAME=<the bot's @handle>   # optional, but invites become one-tap links
```

Restart. The bot starts alongside the web app, or on its own with `make run-telegram` (no HTTP port opened).
Until `OF_TELEGRAM_ADMIN_IDS` is non-empty **the invite gate is inactive and the bot answers everyone** —
set your own id first, then hand out invites with `admin_manage` in the chat. With the handle configured
they arrive as links the recipient only has to tap.

## First things worth trying

1. Ask something ordinary. Watch the answer stream.
2. Ask a second, unrelated question while the first is still writing — both answer at once, each in its own
   bubble.
3. Say "remember that I prefer metric units", then ask something where it matters in a later message.
4. "Every weekday at 9:00 send me a summary of yesterday's notes" — a cron job through `task_create`.
5. Teach it a skill: describe a routine you repeat, ask it to save it, then trigger it later by name.

## Next

- [deployment.md](deployment.md) — running it for real, with TLS
- [../reference/configuration.md](../reference/configuration.md) — every setting
- [embed-the-core.md](embed-the-core.md) — using the core as a library
- [../limitations.md](../limitations.md) — what it will not do

## Code anchors

- `tools/quickstart.py` — `.env` generation
- `docker-compose.local.yml`, `Makefile` — the local stack
- `server/src/octoforge_server/capabilities.py` — the startup report
