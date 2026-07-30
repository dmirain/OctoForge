# Instructions: skills, knowledge, endpoints

The store the agent learns from. One table holds four kinds of record, all found the same way — by
meaning, at request time — and all owned by someone. This is the mechanism behind "capability arrives
without a deploy".

## How it works

A record has a type, a title, content, tags, a version, usage counters, an owner and an author:

| Type | What it holds |
|---|---|
| `skill` | A scenario: how to do something, step by step, naming the tools to use |
| `knowledge` | A fact or a piece of reference material |
| `endpoint` | The contract of one callable HTTP endpoint. **Kept out of default `recall` results** — see below and [endpoints-and-net.md](endpoints-and-net.md) |
| `memory` | Something about one user — see [memory.md](memory.md) |

Each record carries an embedding of its text. The embedding is not part of the DTO: it is an
implementation detail of search.

### Search (`recall`)

`recall(query, k?, type?)` is one ranked search across what is visible to the caller: skills,
knowledge, dataset descriptors and the caller's memories. **Endpoint records are excluded from the
default results** — skills name the endpoints they use and `endpoint_get` resolves a named contract, so
listing endpoints alongside scenarios only added noise. `type=endpoint` searches them explicitly, which
is how a new integration is discovered.

Ranking is three stages, then a diversity pass:

1. **Cosine similarity** over embeddings, vectorized with numpy and executed in a worker thread. This
   matters: a pure-Python loop over 10k records froze the whole event loop for ~850 ms, and `recall` runs
   on nearly every message. The tuple-to-array conversion is chunked as well, because one long C call
   holds the GIL even from a thread.
2. **Exact-title boost.** A record whose title matches the query lands above any merely-similar record
   (the boost is larger than the cosine range can be).
3. **Optional cross-encoder rerank** of the shortlist (`OF_RERANKER_CANDIDATES` candidates → top-k),
   with either a local model or an HTTP reranker.

Finally, **no single type crowds the result out**: each type is capped at `ceil(k/2)` hits so the others
backfill from the oversampled tail, and the cap relaxes when there is nothing else to show — it
diversifies without starving.

Hits come back as whole records, not snippets — a truncated scenario is useless to follow. Returned
records have their `usage_count` incremented. A record stored with an empty or wrong-dimension
embedding scores zero instead of raising, so a deferred embedding or a model change degrades ranking
rather than breaking search.

Stores that can search on their own side implement the runtime-checkable `InstructionVectorSearch`
capability; the service detects it and stops pulling the table into the process. That is the pgvector
path, already prepared.

### Ownership and visibility

| Record state | Who sees it | Who can change it |
|---|---|---|
| Private (`owner_id` = a user) | Its owner | Its owner |
| Public (`owner_id` NULL) | Everyone | Its author, and admins |
| System (`system` = true) | Everyone | The startup sync only |

Rules that follow:

- `instruction_save` always creates a **private** record owned by the caller — ownership comes from the
  session (`ToolContext.user_id`), never from arguments.
- Saving over a public record creates the saver's **shadow copy** rather than mutating what everyone
  sees. The exception is the record's own author: publication transfers visibility, not authorship, so
  the author keeps editing their published record in place.
- `instruction_delete` only deletes the caller's own records. Somebody else's, or a public one, is
  `NotFound`.
- Publication is an admin surface (`publish`), as is cross-user search (`search_all`).
- Agent-facing writes refuse system records with `SystemInstructionError`.
- Uniqueness is `(type, title, owner_id)`, plus a partial unique index over public records — so two users
  may each keep a private "Weekly report" and neither collides with a public one.

### System records and the overlay

The system-owned slice is declarative: `CORE_SYSTEM_SKILLS` in the core and `WEB_SYSTEM_SKILLS` in the
web package list the records an installation must have. At startup `sync_system_registry` upserts them
as `system=true` (adopting same-named legacy public records) and deletes system records that vanished
from the registry. User records are never touched.

`OF_SYSTEM_SKILLS_SOURCE=file:...` applies a JSON overlay before the sync: it can replace a record's
content, append to it, or add new records. This is how one image serves deployments that need different
trigger phrases or a different house style — no rebuild, no fork.

### Saving is lenient about embeddings

If the embedding backend is unreachable at save time, the record is stored with an empty embedding
rather than being lost. `reembed_missing()` at startup fills those in. The fact survives; only its
searchability is delayed.

## Invariants

- **Ownership comes from the session**, never from tool arguments.
- **A save over a public record never mutates it for everyone** (except for its author).
- **System records are only written by the startup sync.**
- **Search returns whole records**, and never returns another user's private record.
- **Ranking never raises on bad vectors** — it scores them zero.
- **Ranking never runs inline on the event loop.**
- **The store is authoritative.** There is no derived index to rebuild, so there are no
  reindex/shadow-publish failure modes.
- **`recall` is one call over the kinds worth mixing** — endpoints are excluded by default; a `type`
  filter narrows it, applied before ranking.
- **No type takes more than `ceil(k/2)` of the hits** unless there is nothing else to return.

## Configuration

| Variable | Effect |
|---|---|
| `OF_EMBEDDING_*` | The embeddings backend; without one, search and save are unavailable |
| `OF_INSTRUCTIONS_TOP_K` | Default number of records `recall` returns |
| `OF_RERANKER_MODEL` / `OF_RERANKER_API_KEY` / `OF_RERANKER_CANDIDATES` | Optional rerank stage and its shortlist size |
| `OF_SYSTEM_SKILLS_SOURCE` | `file:` JSON overlay over the system registry |

## Failure modes

| Situation | Outcome |
|---|---|
| No embeddings backend | `recall`, save and the system sync are unavailable; the app still starts and says so |
| Embedding call fails during save | Record stored with an empty embedding; startup sweep re-embeds it |
| Embedding model changed (different dimension) | Old vectors score zero until re-embedded |
| Reranker unavailable | Falls back to cosine ordering |
| Two records with the same title for one owner | Prevented by the uniqueness constraint; the upsert replaces content and bumps the version |
| Agent tries to edit a system record | `SystemInstructionError`, explained back to the model |
| Overlay file unreadable or malformed | Logged warning, overlay ignored, built-in registry synced as-is |
| Overlay `append` names a record the registry does not have | Logged warning; the rest of the overlay still applies |

## Code anchors

- `core/src/octoforge_core/instructions/api.py` — `Instruction`, `InstructionType`, the store/service
  ports, `InstructionVectorSearch`
- `core/src/octoforge_core/instructions/local.py` — the service: embed, rank, boost, rerank
- `core/src/octoforge_core/instructions/ranking.py` — the scoring functions
- `core/src/octoforge_core/instructions/store.py` — SQL store, visibility predicates, uniqueness
- `core/src/octoforge_core/instructions/registry.py` — the declarative system registry and its sync
- `web/src/octoforge_web/skill_overlay.py`, `web/src/octoforge_web/system_skills.py` — the overlay and
  the web-side registry
- `core/src/octoforge_core/instructions/tools.py` — `recall`, `instruction_save`, `instruction_delete`
- `core/tests/test_instructions_local.py`, `core/tests/test_ranking.py`,
  `core/tests/test_instructions_store.py`, `core/tests/test_instructions_registry.py`
