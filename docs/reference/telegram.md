# Telegram

A bot on the same conversation engine as the web chat: long-polling in, rendered events out. It is the
fastest way to see the agent working in a real messenger, and it exercises the parts of the design that a
single-page client does not — reply threading, voice, albums, per-user gating.

## How it works

No bot framework: a raw httpx client against the Bot API (`telegram/client.py`), a poller
(`telegram/poller.py`) and a per-chat bridge (`telegram/bridge.py`).

- **Channel** `"telegram"`, **user id** `tg:<telegram id>`. Private chats only.
- **Ingestion** does not happen inside the poll loop: an update is put on a per-user queue and processed
  from there, so one slow user (a long voice download, a vision call) never delays everyone else's updates.
- **Rendering** is one throttled draft message per exchange. Deltas edit that draft at most every
  `OF_TELEGRAM_EDIT_THROTTLE_SECONDS`; each answer replies to the message that asked it, which is what
  keeps concurrent answers readable.
- **Reply resolution is deterministic.** A user replying to a bot message names the exchange directly, so
  no router call is needed.

### Message kinds

| Incoming | Becomes |
|---|---|
| Text | A user message; routed normally |
| Reply to a bot message | A user message attached to that exchange, no router call |
| Forwarded message | Material with attribution baked into the text — never an obligation |
| Photo with a caption | A user message; the image is described by the vision tier |
| Photo without a caption | Material |
| Album | One message keeping every page |
| Voice note | Transcribed and submitted **as the user's own words** |

See [vision-and-speech.md](vision-and-speech.md) for the modality details and
[exchanges.md](exchanges.md) for what "material" implies.

### Outgoing formatting

Agent answers are Markdown; Telegram is not. `telegram/markdown.py` converts to Telegram HTML with a
plain-text fallback, and chunks long output at the 4096-character limit without splitting a tag.

A final answer containing a table, a task list, `<details>` or block math is upgraded in place to a native
**Rich Message** (Bot API 10.1, `telegram/rich.py`) — those constructs degrade badly in the HTML path. The
streaming draft stays on the HTML path; only the final is upgraded, at most one message, up to 32,768
characters, and it falls back to the HTML version if the API refuses.

### Access control

Access is invite-based (`telegram/invites/`), in its own SQLite (or Postgres) database with its own schema
and no Alembic chain — invites are a Telegram-specific concept and core knows nothing about them.

- Admins (`OF_TELEGRAM_ADMIN_IDS`) always pass.
- Everyone else needs `/start <code>` with a code that is `PENDING` and not older than
  `OF_TELEGRAM_INVITE_TTL_SECONDS`. Codes move `PENDING → CLAIMED`, or `REVOKED`.
- **The gate only activates once the admin list is non-empty.** With no admins configured the bot answers
  everyone — a deliberate first-run behavior that the startup capability report flags in capitals.

Past the gate, every message upserts the sender's profile (name, `@username`) into a members table, so the
console and the `admin_manage` tool can show who a `tg:<id>` actually is and which invite they came through.

### Commands and the admin tool

`/start [code]` joins; `/secrets` returns a one-time link to the secret form (see
[secrets.md](secrets.md)). Admins additionally get the `admin_manage` tool inside the chat — list users
with names and invite attribution, generate, revoke and restore invites, search instructions across users,
publish one. It hides itself from non-admins through the registry's visibility hook.

### Running it

Alongside the web app (same process, one composition root) or standalone:
`python -m octoforge_web.telegram` — no HTTP port is opened at all, only outbound connections.

One process per bot token. A second poller against the same token steals updates from the first, which is
why the local quickstart stack keeps the bot off unless `OF_QUICKSTART_TELEGRAM_TOKEN` is set explicitly.

## Invariants

- **Private chats only.** Groups, threads and channels are not handled.
- **One draft message per exchange**, and each answer replies to its question.
- **An explicit reply never spends a router call.**
- **Forwarded content is material**, and material never carries authority (it cannot cancel an exchange).
- **A voice message is the user speaking**, not material.
- **Ingestion is queued per user**, so the poll loop never blocks on one chat's work.
- **The invite gate activates only with a configured admin list**, and the capability report says so.
- **Only one process may poll a token.**
- **Bot API URLs are never logged** — the httpx logger is pinned to WARNING because the URL contains the
  token.

## Configuration

| Variable | Effect |
|---|---|
| `OF_TELEGRAM_BOT_TOKEN` | The bot; empty means the adapter does not start |
| `OF_TELEGRAM_ADMIN_IDS` | Admins; while empty the invite gate is inactive |
| `OF_TELEGRAM_INVITE_TTL_SECONDS` | How long a code stays claimable |
| `OF_TELEGRAM_DATABASE_URL` | The invite/member database |
| `OF_TELEGRAM_POLL_TIMEOUT_SECONDS` | Long-poll timeout |
| `OF_TELEGRAM_EDIT_THROTTLE_SECONDS` | Minimum interval between draft edits |
| `OF_TELEGRAM_RICH_MESSAGES` | Whether qualifying finals are upgraded to Rich Messages |
| `OF_VISION_*`, `OF_STT_*`, `OF_VOICE_MAX_SECONDS` | Images and voice |

## Failure modes

| Situation | Outcome |
|---|---|
| Two processes polling one token | Updates are split unpredictably between them — run exactly one |
| Rich Message rejected by the API | Falls back to the HTML rendering of the same answer |
| Answer longer than the message limit | Chunked without breaking markup |
| Edit throttled or rate-limited | The draft catches up on the next allowed edit; the final always lands |
| Vision or transcription unavailable | The message still arrives, with a placeholder or a "text only" notice |
| Unknown or expired invite code | Explained to the user; access denied |
| No admins configured | The bot answers everyone until an admin id is set |
| Bridge dies mid-answer | The terminal was not accepted by any subscriber, so the result stays in the outbox and is delivered on reconnect |

## Code anchors

- `web/src/octoforge_web/telegram/client.py` — the Bot API client and limits
- `web/src/octoforge_web/telegram/poller.py` — long polling, per-user queues, commands, gating
- `web/src/octoforge_web/telegram/bridge.py` — event rendering, drafts, throttling, reply threading
- `web/src/octoforge_web/telegram/markdown.py`, `telegram/rich.py` — formatting and the Rich Message path
- `web/src/octoforge_web/telegram/images.py` — image refs and resolution
- `web/src/octoforge_web/telegram/invites/` — invite store, member directory
- `web/src/octoforge_web/telegram/admin.py` — the `admin_manage` tool
- `web/src/octoforge_web/telegram/__main__.py` — the standalone entry point
- `web/tests/test_telegram_*.py` — poller, bridge, markdown, rich, invites, images, standalone
