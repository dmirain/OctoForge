# Restructure plan: surfaces as installable modules

Working document for the in-flight restructuring. Not part of `docs/` on purpose —
`docs/CONVENTIONS.md` says documentation describes what the system does now, and this
describes what it is about to do. Delete it when the work lands.

Agreed 2026-08-01.

## Goal

Separate every surface (Telegram, admin console, web UI) from the HTTP service that
serves core, so each installs on demand. The service itself is internal: reachable from
the surfaces, not from the open internet.

Two composition modes, chosen per deployment rather than baked in:

- **in-process** — the surface is a library the service loads;
- **over HTTP** — the surface is its own process talking to the service.

The Telegram ingestion node is the first case of the second mode. Building it is *not*
part of this plan; making it possible is.

## Target layout

```
core/                     octoforge-core        typed library, no framework (unchanged)

server/                   octoforge-server      the internal service over core
  src/octoforge_server/
    app.py                FastAPI factory: middleware, health, mounting
    runtime.py            composition of CORE services only
    config.py             service settings: LLM, database, limits, credentials
    auth.py               operator and service credentials
    deps.py               request-scoped dependencies
    channels.py           known channels
    surfaces.py           the Surface port and the registry of installed ones
    capabilities.py       startup capability report
    prompts.py  audit.py  secret_links.py  skill_overlay.py  system_skills.py
    api/
      dialog.py  cron.py  secrets.py  schemas.py  sse.py
    static/
      secrets.html        part of the secrets mechanism, not a UI

surfaces/telegram/        octoforge-telegram
  src/octoforge_telegram/
    client.py             Bot API client
    models.py             update models
    schema.py             the surface's own database base
    invites/              invite store, member directory
    ingest/               INGESTION — able to run as its own process
      __main__.py         standalone entry point
      poller.py
      membership.py       the access gate
      media.py            download, describe images, transcribe speech
      gateway.py          the DialogGateway port and its HTTP adapter
    render/               LIBRARY — loaded by the service
      bridge.py
      drafts.py
      markdown.py
    tools/admin.py        the admin_manage tool
    admin_routes.py       /api/admin/telegram/users — temporary, dies with identity

surfaces/admin/           octoforge-admin
  src/octoforge_admin/
    routes.py             /api/admin/*
    static/admin.html

surfaces/webui/           octoforge-webui
  src/octoforge_webui/
    routes.py
    static/index.html

deploy/                   octoforge-deploy      the assembled deployment
  src/octoforge_deploy/
    main.py               the real composition root
```

`deploy/` exists because the service must not import a surface — otherwise the boundary is
a fiction. Something above both has to wire them, and that something is the deployment.

## The Surface port

```python
class Surface(Protocol):
    name: str
    def routers(self) -> Sequence[APIRouter]          # what to mount
    def static(self) -> Sequence[tuple[str, Path]]    # what to serve
    def dialog_surface(self) -> DialogSurface | None  # what renders a dialog
    def tools(self) -> Sequence[Tool]                 # what the agent gains
    async def start(self) -> None                     # background work
    async def aclose(self) -> None
```

`dialog_surface()` already exists (`DialogSurface`, added 2026-07-31). The rest is the same
idea for routes, static files and tools.

## Stages

Each stage is one commit with a green gate, so any of them can be reverted on its own.

### 1. The seam, without moving anything — DONE

Introduce `Surface` inside the current package and re-express Telegram, the admin console
and the web UI as surfaces. No file moves, no behavior change.

Cut the admin → Telegram edge here: `/telegram/users` moves to the Telegram surface's own
routes. That edge is what blocks the admin console from moving at all.

The point is to learn whether the abstraction fits before 60 files depend on it.

### 2. The physical move — DONE

Create the distributions, move the files, update imports, `Makefile`, `Dockerfile`, CI.
No logic changes. The gate is a strong verifier here: imports either resolve or they do
not, and the suites either pass or they do not.

### 3. Boundaries under test — DONE (in `deploy/tests/test_surfaces.py`)

Extend the import-boundary tests:

- core imports nothing above itself;
- the service imports no surface;
- no surface imports another surface;
- `deploy` may import everything.

Without this the structure drifts back — that is exactly how `/telegram/users` appeared.

### 4. Settings split — DONE

Each surface owns its settings; `deploy` assembles them. Separate from the move because
`Settings` is used everywhere and mixing the two would hide which change broke what.

All four stages are done on `feat/surfaces-as-modules`. What follows is the next
work, and belongs on its own branches.

## After this plan, on their own branches

**Identity.** A `users` table with an opaque id, plus `user_identities`
(`user_id`, `surface`, `external_id`, `details` JSON, unique on surface+external_id).
A JSON column on `users` was rejected: it cannot enforce "one Telegram account belongs to
at most one user", and per-surface expression indexes would need a migration each, which
is the thing a JSON column is chosen to avoid. Email becomes a column on `users` once
registration exists — it is the account, not a surface.

The delivery address moves into the identity row. A surface may still derive a chat id
from its own external id; only the *core* id becomes opaque, which is what makes
`chat_id_from_user_id` impossible to write against it.

A new identity never creates a user by itself — an invite carries the core user id.
Otherwise a second surface silently creates a second user and merging dialogs, memories
and skills becomes necessary; with invites it never is.

Identities deactivate rather than disappear, so a revoked account keeps its history.

**Ingestion node.** `DialogGateway` with two adapters, the poller as a deployment choice.
Needs live verification against a real bot.

## Rollback

Nothing merges to `main` until it is asked for. The current feature branch stays as it is;
this work branches from it. Reverting means not merging, or reverting one stage's commit.
