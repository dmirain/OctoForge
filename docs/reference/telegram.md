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

Agent answers are Markdown and Telegram renders Markdown natively, so nothing is converted on the way
out: every answer is a **Rich Message** (Bot API 10.1), sent with `sendRichMessage` and edited in place
with `editMessageText` as it streams. What the agent wrote is what the chat receives.

The limit that applies is therefore the Rich Message one — **32,768 characters**, eight times the
plain-text budget — plus 500 blocks, 16 levels of nesting, 50 media attachments and 20 table columns. An
answer past 32,768 characters is sealed and continued in a fresh message, cut on a line boundary so a
table row or list item is never torn in half.

This replaced a Markdown→HTML conversion that chunked at 4,096 characters and upgraded only qualifying
finals to Rich Messages. The conversion existed to survive a renderer that could not do tables, and the
chunking it needed is what broke them: an answer over 4,096 characters was split across messages, and a
split answer was no longer eligible for the upgrade, so its table arrived as raw pipes and letters.

Plain notices — greetings, refusals, invite texts — still go out as ordinary text messages; they carry
no formatting to preserve.

### A person, not an account

A dialog belongs to a person, and this surface records which Telegram account is theirs. Two
consequences the surface has to live with:

- **Where to write is looked up, not derived.** A person's id carries no structure, so the chat id
  comes from their identity. `chat_id_from_user_id()` still exists, and is only for this surface's
  own records — the invite store and the member directory file people under `tg:<id>`, which is a
  Telegram id and always was.
- **Changing accounts keeps everything.** Re-seating moves the identity; the dialog, its history and
  the person's skills and secrets are filed under the person and do not move at all.

An account arriving unknown becomes a new person. Attaching it to an *existing* one is what an
invite carrying a user id does — if first contact could do it, a mistake would hand a stranger
somebody's dialogs.

### The bridge follows the actor

A chat bridge is attached when its dialog's actor is built and dropped when the dialog leaves this
process — the `DialogSurface` port. Rendering therefore does not depend on the user having written
recently: a cron firing, a background result, or an answer this process has just inherited all have
somebody to deliver them.

### Drafts survive a move

A draft is which Telegram message an answer is being written into. It lives in the bridge's memory,
which is enough while one process owns a dialog for its whole life — and wrong the moment dialogs
move between processes: the new owner would start a *second* message, leaving the user with a
truncated answer and a complete one under it.

So the message id is written down, in the surface's own database (`telegram_drafts`), keyed by the
exchange. Only what cannot be recreated is stored — which message, what it replies to, how many
chunks of a long answer went before it. The text is not: the new owner re-answers and edits the same
message until it holds the finished reply.

Written once per message *created*, not per edit; read when a bridge attaches; dropped when the
answer settles. A draft with no exchange (a broker notice, a background result) is one-shot and never
remembered.

### Access control

Access is invite-based (`telegram/invites/`), in its own SQLite (or Postgres) database with its own schema
and no Alembic chain — invites are a Telegram-specific concept and core knows nothing about them.

- Admins (`OF_TELEGRAM_ADMIN_IDS`) always pass.
- Everyone else needs `/start <code>` with a code that is `PENDING` and not older than
  `OF_TELEGRAM_INVITE_TTL_SECONDS`. Codes move `PENDING → CLAIMED`, or `REVOKED`.
- With `OF_TELEGRAM_BOT_USERNAME` set, `admin_manage` hands out the invite as a markdown deep link
  (`[@bot](https://t.me/<bot>?start=<code>)`) instead of a bare code: opening it starts the bot and
  claims the invite in one tap, because Telegram delivers the payload as `/start <code>`. The handle
  is configuration rather than a constant — only the Bot API knows a bot's public name, and guessing
  it in code would point invitations at somebody else's chat. Without it the tool returns the code
  and says which variable would improve that.
- **The gate only activates once the admin list is non-empty.** With no admins configured the bot answers
  everyone — a deliberate first-run behavior that the startup capability report flags in capitals.

Past the gate, every message upserts the sender's profile (name, `@username`) into a members table, so the
console and the `admin_manage` tool can show who a `tg:<id>` actually is and which invite they came through.

### Commands and the admin tool

`/start [code]` joins; `/secrets` returns a one-time link to the secret form (see
[secrets.md](secrets.md)). Admins additionally get the `admin_manage` tool inside the chat — list users
with names and invite attribution, generate, revoke and restore invites, search instructions across users,
publish one. It hides itself from non-admins through the registry's visibility hook, and every action it
performs (including a refused one) writes an audit line naming the admin's Telegram id.

### Running it

Alongside the web app (same process, one composition root) or standalone:
`python -m octoforge_deploy.telegram_only` — no HTTP port is opened at all, only outbound connections.

One process per bot token. A second poller against the same token steals updates from the first, which is
why the local quickstart stack keeps the bot off unless `OF_QUICKSTART_TELEGRAM_TOKEN` is set explicitly.

## Invariants

- **Private chats only.** Groups, threads and channels are not handled.
- **One draft message per exchange**, and each answer replies to its question.
- **A draft outlives the process that started it**, so a dialog that moves keeps writing into the
  message the user is already looking at — see [dialog-ownership.md](dialog-ownership.md).
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
| `OF_TELEGRAM_BOT_USERNAME` | The bot's public handle (`name`, `@name` or a t.me URL). Turns generated invites into one-tap deep links |
| `OF_TELEGRAM_INVITE_TTL_SECONDS` | How long a code stays claimable |
| `OF_TELEGRAM_DATABASE_URL` | The surface's own database: invites, member profiles, live drafts |
| `OF_TELEGRAM_POLL_TIMEOUT_SECONDS` | Long-poll timeout |
| `OF_TELEGRAM_EDIT_THROTTLE_SECONDS` | Minimum interval between draft edits |
| `OF_VISION_*`, `OF_STT_*`, `OF_VOICE_MAX_SECONDS` | Images and voice |

## Failure modes

| Situation | Outcome |
|---|---|
| Two processes polling one token | Updates are split unpredictably between them — run exactly one |
| Rich Message rejected by the API | The client retries transient failures and the bridge retries the final flush once; a persistent refusal is logged loudly and the answer does not land |
| Answer longer than 32,768 characters | Continued in a fresh message, cut on a line boundary |
| Edit throttled or rate-limited | The draft catches up on the next allowed edit; the final always lands |
| Vision or transcription unavailable | The message still arrives, with a placeholder or a "text only" notice |
| Unknown or expired invite code | Explained to the user; access denied |
| No admins configured | The bot answers everyone until an admin id is set |
| `OF_TELEGRAM_BOT_USERNAME` unset or wrong | Invites are handed out as bare codes; a wrong handle produces a link into another bot's chat, where the code cannot be claimed |
| Bridge dies mid-answer | The terminal was not accepted by any subscriber, so the result stays in the outbox and is delivered on reconnect |

## Code anchors

- `surfaces/telegram/src/octoforge_telegram/client.py` — the Bot API client and limits
- `surfaces/telegram/src/octoforge_telegram/poller.py` — long polling, per-user queues, commands, gating
- `surfaces/telegram/src/octoforge_telegram/bridge.py` — event rendering, drafts, throttling, reply threading
- `surfaces/telegram/src/octoforge_telegram/images.py` — image refs and resolution
- `surfaces/telegram/src/octoforge_telegram/invites/` — invite store, member directory
- `surfaces/telegram/src/octoforge_telegram/drafts.py` — where a live answer is being written
- `surfaces/telegram/src/octoforge_telegram/schema.py` — the surface's declarative base
- `surfaces/telegram/src/octoforge_telegram/admin.py` — the `admin_manage` tool
- `deploy/src/octoforge_deploy/telegram_only.py` — the standalone entry point
- `deploy/tests/test_telegram_*.py` — poller, bridge, markdown, rich, invites, images, standalone
