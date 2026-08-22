# HTTP API

A thin FastAPI adapter over the conversation engine: submit a message, subscribe to events, manage cron
jobs, read the admin model, and serve the two static pages. Everything is behind one operator credential
except the health probes and the token-authenticated secret form.

## How it works

`create_app()` builds the application; the lifespan enters `runtime()` (the composition root) and exposes
the assembled services on `app.state`, which the dependency providers in `deps.py` read. Handlers
therefore contain no wiring: they take ports and call them.

### Dialog endpoints

| Method and path | Purpose |
|---|---|
| `POST /api/dialog/messages` | Submit a user message. Body: `content`, optional `client_message_id` (idempotency key), optional `reply_to_exchange_id` (skips the router), plus what only a surface knows: `kind` (`own` or `material`), `origin`, `attachments` (`[{kind, ref}]`). Answers `202 Accepted` |
| `POST /api/dialog/cancel` | Stop the dialog's live answer runs |
| `GET /api/dialog/events` | Subscribe to the dialog's event stream over SSE |

The dialog is selected by the `X-User-Id` header and the `X-Channel` one. Together they name an
account on a surface, not a person: surfaces number their users independently, so `42` on one is not
`42` on another. The service turns the pair into a person, minting one on first contact — who may
talk at all was decided before the request arrived, by an invite gate or by the credential in front
of the service. `X-Channel` defaults to the
channel the process declares (`web` for the HTTP app), which is why existing clients need not know it
exists; a value outside the surfaces this deployment serves is refused with 400 rather than quietly
given a dialog of its own. A per-request channel is what lets one process serve several surfaces
instead of needing a separate fleet for each. A dialog is created
on first contact.

### Identity endpoints

| Method and path | Purpose |
|---|---|
| `PUT /api/identity/profile` | Mirror what a surface currently calls an account. Body: `name`, `username`. Selected by `X-User-Id`/`X-Channel` like a dialog; mints the person on first contact and answers `204` |

This is how the out-of-process Telegram ingestion node names its people: names key on people, and
that node cannot key anything by person. Deliberately **no status gate** — a person still waiting is
precisely the one whose name the operator needs in the console's queue, and recording a name burns
no model call, so there is nothing for a banned account to abuse. The identity's name follows the
surface on every call; the person's own `users.name` is seeded only while still empty (see
`core/src/octoforge_core/identity/api.py`).

Submitting is deliberately asynchronous: the message is accepted, and the answer arrives on the event
stream. A retry with an already-seen `client_message_id` is accepted and skipped, so a flaky network does
not double-run anything.

### The SSE stream

Each frame is a JSON object with `seq`, `dialog_id`, `exchange_id` and the event's own fields. Event types
mirror the loop's: `text_delta`, `assistant_message`, `tool_call_requested`, `tool_call_completed`,
`tool_call_failed`, `retry_scheduled`, `finished`, `cancelled`, `failed`, plus the actor's
`process_started` / `process_completed` markers and `iteration_started`.

`exchange_id` is what makes concurrent answers usable: a client keeps one bubble per exchange and appends
deltas to the right one. A comment frame (`: heartbeat`) is sent every 15 seconds of silence so proxies do
not drop the connection.

Unsubscribing happens automatically when the client disconnects.

### Cron endpoints

`POST /api/cron/jobs` (parameters in the query string, because stored endpoints call it through
`external_call`, which has no body), `GET /api/cron/jobs`, `DELETE /api/cron/jobs/{id}`,
`POST /api/cron/jobs/{id}/pause`, `POST /api/cron/jobs/{id}/resume`. All owner-scoped by `X-User-Id`.

**Operator-only, like the rest of the surface.** These are behind the same single credential, and
`X-User-Id` is a trusted string — so anyone who can reach them can schedule work as any user. That is
not a gap in the router but the same trust model the whole HTTP surface has: the agent reaches these
through `OF_SELF_BASE_URL` on loopback, and a deployment exposing them to end users must put an
authenticating proxy in front. See [../security.md](../security.md).

### Admin endpoints

`GET /api/admin/...` — paginated read-only listings across users (totals, dialogs, messages, tasks, cron,
instructions, datasets and their records, memories, summaries, exchanges, Telegram users) plus two
mutations that go through owner-scoped services: publishing an instruction and toggling a cron job. See
[admin-console.md](admin-console.md).

### Secret endpoints

`POST /api/secrets/session`, `POST /api/secrets/set`, `POST /api/secrets/delete` — authorized by the
one-time token from the Telegram `/secrets` flow, not by the operator credential, because dialog users do
not have one. The token travels in request bodies and reaches the browser in the link's fragment, so it
never appears in a URL a proxy would log. See [secrets.md](secrets.md).

### Static pages and probes

| Path | What |
|---|---|
| `/` | The streaming chat UI |
| `/admin.html` | The operator console |
| `/secrets.html` | The secret-entry form (token-authenticated) |
| `/docs`, `/openapi.json` | FastAPI's own documentation |
| `/health` | Liveness |
| `/health/ready` | Readiness — checks the database and that the claim heartbeat still runs |

### Authentication

One HTTP Basic credential guards the whole surface, applied as middleware in `create_app` rather than as a
per-router dependency, so it also covers static files and `/docs`. Open paths: `/health`,
`/health/ready`, `/secrets.html` and `/api/secrets/*`.

Before the credential is checked, the middleware refuses **cross-site state-changing requests**: a browser
attaches Basic credentials automatically, so a form on another site could act as the operator. Origin is read
from `Sec-Fetch-Site` (falling back to `Origin`); requests with neither header are not browsers and pass.

Verification itself runs in a worker thread and behind a per-client failure budget, so a flood of wrong
passwords cannot stall the event loop; a verified credential is cached briefly. See
[../security.md](../security.md).

The password is stored as a PBKDF2-HMAC-SHA256 hash in the format `pbkdf2_sha256:iterations:salt:digest`
(`:` rather than `$` because docker compose interpolates `$` in `.env`), verified in constant time. An
empty hash answers **503** — it fails closed, never open.

A second credential (`OF_SERVICE_USERNAME` / `OF_SERVICE_PASSWORD_HASH`) opens the relay's own
traffic — `/api/dialog/*`, `/api/media/*` and `PUT /api/identity/profile` — and nothing else. It
exists so a process that merely relays a surface's traffic — the Telegram ingestion
node — need not carry operator power: a compromise there must not become a compromise of the console,
the instructions and the secret store. Each credential has its own verification cache, so one verified
on a dialog request cannot satisfy an admin one. Leaving it unset turns it off.

The two sides of that credential are configured separately, and confusing them fails quietly: the
service verifies against `OF_SERVICE_PASSWORD_HASH`, while the relay presents `OF_SERVICE_PASSWORD`.
A relay that sends the hash is sending a wrong password, and a rejected relay looks exactly like a
silent one — the user gets no answer and only a 401 in a log records why.

This authenticates the **operator**, not the agent's users. `X-User-Id` selects the dialog and is a trusted
string: front the deployment with a proxy that authenticates people and sets that header. See
[../security.md](../security.md).

## Invariants

- **Handlers hold no wiring.** Everything comes from `app.state` through `deps.py`.
- **Message submission is idempotent** on `client_message_id`.
- **Every SSE frame carries its `exchange_id`.**
- **The gate is middleware**, so no route can be added outside it by accident; only the explicit open
  paths bypass it.
- **An empty credential means 503**, not open access.
- **`/health` needs no credential** (container healthchecks and uptime monitors depend on it), and
  `/health/ready` additionally touches the database and reports `ownership: stale` (503) when the
  process holds dialogs whose claims it stopped refreshing — see
  [dialog-ownership.md](dialog-ownership.md).
- **Cron creation takes query parameters**, because the agent's own `external_call` cannot send a body.

## Configuration

| Variable | Effect |
|---|---|
| `OF_ADMIN_USERNAME`, `OF_ADMIN_PASSWORD_HASH` | The operator credential; empty hash = 503 |
| `OF_SELF_BASE_URL` | How the agent addresses this API (also allowlisted in the SSRF guard) |
| `OF_PUBLIC_BASE_URL` | Origin used in user-facing links |

## Failure modes

| Situation | Outcome |
|---|---|
| No credential configured | Every guarded request answers 503 with "admin credentials are not configured" |
| Wrong credential | 401 with a Basic challenge; the attempt is logged with the client address |
| Repeated wrong credentials | 429 after five failures, for a cooldown, without hashing anything |
| Cross-site POST/DELETE from a browser | 403, before authentication |
| Missing `X-User-Id` | 400 |
| Unknown `X-Channel` | 400 |
| Client disconnects mid-stream | Subscription removed; the run keeps going and its result is delivered through the outbox on the next subscribe |
| Slow client | Stream events dropped for that subscriber; terminals still delivered |
| Database unavailable | `/health/ready` fails (logged), `/health` still answers |
| Claim heartbeat stalled | `/health/ready` answers 503 with `ownership: stale`; the balancer drops the pod until a beat completes |

## Code anchors

- `deploy/src/octoforge_deploy/main.py` — `create_app()`, the middleware gate, health probes, static mounts
- `server/src/octoforge_server/api/dialog.py` — messages, cancel, SSE
- `server/src/octoforge_server/api/identity.py` — the profile mirror
- `server/src/octoforge_server/api/sse.py` — frame encoding and event payloads
- `server/src/octoforge_server/api/cron.py`, `surfaces/console/src/octoforge_console/routes.py`, `api/secrets.py` — the other routers
- `server/src/octoforge_server/api/schemas.py` — request and response models
- `server/src/octoforge_server/auth.py` — hashing, verification, the open-path rules
- `server/src/octoforge_server/deps.py` — dependency providers
- `deploy/tests/test_dialog_api.py`, `deploy/tests/test_sse.py`, `deploy/tests/test_admin_api.py`,
  `deploy/tests/test_cron_api.py`, `deploy/tests/test_secrets_api.py`
