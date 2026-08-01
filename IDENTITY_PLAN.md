# Identity: one user, several surfaces

Working document. Not under `docs/` — it describes what is about to be true. Delete it
when the work lands. Agreed 2026-08-01.

## What is wrong today

A user *is* their transport handle. `dialogs.user_id` holds `tg:12345`, and so do
`tasks`, `cron_jobs`, `secrets`, `datasets`, `dataset_records` and `instructions.owner_id`.

Two consequences:

- **A person cannot change their Telegram account.** Everything they own is filed under
  the old handle, and there is nowhere to say the new one is the same person.
- **The handle doubles as the delivery address.** `chat_id_from_user_id()` parses the chat
  id back out of the identity, in six places. That is why re-seating is not merely
  unimplemented but impossible: change who they are and there is nowhere to write to.

## Shape

```
users
  id            opaque, generated
  email         the cross-cutting identifier once registration exists; empty until then
  created_at

user_identities
  user_id       -> users.id
  surface       'telegram', 'web', ...
  external_id   what that surface calls them
  details       JSON: surface-specific extras
  active        a revoked identity keeps its history instead of vanishing
  created_at, updated_at
  UNIQUE (surface, external_id)
```

Two tables, not one with a JSON column of surface ids. The one invariant that matters —
**one Telegram account belongs to at most one user** — is something only a unique
constraint can hold. A JSON column cannot, and per-surface expression indexes would need a
migration each, which is the thing a JSON column would be chosen to avoid.

Email belongs on `users`: it is the account, not a surface.

## Rules

- **A new identity never creates a user on its own where one already exists.** An invite
  carries the core user id; claiming it links the new identity instead of minting a second
  user. Without this, a second surface silently creates a second person, and merging
  dialogs, memories and skills becomes necessary — a job an order of magnitude harder than
  re-seating, and lossy. With it, merging is never needed.
- **First contact with nobody to link to does create a user.** That is what an invite
  without a named user means, and what the web surface does today.
- **Re-seating moves an identity, not a user.** The core id never changes; the identity's
  `external_id` does. Everything filed under the core id follows for free.
- **The delivery address lives with the identity.** A surface may still derive a chat id
  from its own external id — that is its own knowledge, correctly placed. What must become
  impossible is deriving it from the *core* id, and an opaque id is what makes it so.
- **Identities deactivate rather than disappear**, so a revoked account keeps its history.

## Resolution

The service resolves at the edge: `X-User-Id` plus `X-Channel` are (external id, surface),
and the identity store turns them into a core id. Surfaces stay thin — each one sends what
it knows, which is what it calls the user — and the actor keeps taking a user id that is
now always a core id.

## Migration

One migration, one transaction:

1. create the two tables;
2. for every distinct id across the seven columns, mint a user with an opaque id;
3. record its identity: `tg:<n>` becomes (telegram, `<n>`), anything else (web, as-is) —
   a historical fact about how ids used to be minted, which is exactly what a migration is
   for;
4. rewrite the seven columns.

Renumbering rather than reusing the old strings as core ids on purpose. Reuse is cheaper
and keeps the disease: an id that can be parsed *will* be parsed, and `chat_id_from_user_id`
would be writable again the week after. Twenty users make the honest version affordable.

The Telegram surface's own tables (`members.user_id`, `invites.claimed_by`) keep holding
Telegram ids: they are that surface's business, and the identity table is what joins them
to a person.

## Order

1. Core identity module — DONE: ports, models, store.
2. The migration — DONE, verified on a copy of the production database before anything else.
3. Resolution at the edge — DONE.
4. Telegram — DONE: external id, delivery address from the identity, invites that link.
5. The console — DONE: a user is a person with identities, not a handle.
6. Docs — DONE.

## Rollback

Its own branch, nothing merged until asked. A backup is taken before the migration is run
anywhere real, and the migration is exercised against a copy of production first — this is
the first change in this sequence that touches data, and a mistake here is not undone by a
revert.
