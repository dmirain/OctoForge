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

Retrieval runs **two retrievers**, because they fail in opposite ways.

- **Vector search** finds records *about* the query. On Postgres with pgvector this happens in the
  database, ordered by cosine distance; otherwise the visible rows are ranked in process with numpy
  in a worker thread. That thread matters: a pure-Python loop over 10k records froze the whole event
  loop for ~850 ms, and `recall` runs on nearly every message. The tuple-to-array conversion is
  chunked as well, because one long C call holds the GIL even from a thread.
- **Lexical search (BM25)** finds records that literally *say* it. This is the half embeddings are
  bad at: a product name, an error code, an API field, a rare acronym — cases where nearest-neighbour
  search cheerfully returns four documents on the topic that never mention the term. Postgres
  provides it through `pg_textsearch`, SQLite through FTS5; without either, recall is embeddings only.

**The two lexical engines are not equivalent, and the difference is visible to users.** Postgres stems
through `russian_unaccent`, so "задача" finds "задачи". SQLite has no Russian stemmer; the closest
available tokenizer is `trigram`, which matches substrings — it finds "задачи" from "задач" and
"договора" from "договор", but not from "задача", which is not a substring of anything there. Latin
technical terms behave identically on both. The startup report names which engine is live rather than
just saying "on".

Two BM25 indexes cover a record, title and content, not one over their concatenation: BM25 normalizes
relevance by document length, so a two-token title folded into a two-hundred-token body would carry
almost no weight. Kept apart, a record whose term appears only in its title still comes back.

Where both retrievers exist their orderings are merged by **Reciprocal Rank Fusion** (k=60) rather
than a weighted sum of scores. Cosine is bounded in [-1, 1] and BM25 is unbounded and
corpus-dependent, so any normalization between them would need retuning as the corpus grows; RRF
reads only positions. A record both retrievers like outranks one that only a single retriever loves.

Then, unchanged by any of this:

1. **Exact-title boost.** A record whose title matches the query lands above any merely-similar
   record — the boost is larger than the cosine range, and far larger than any fused score can be.
2. **Optional cross-encoder rerank** of the shortlist (`OF_RERANKER_CANDIDATES` candidates → top-k),
   with either a local model or an HTTP reranker.
3. **No single type crowds the result out**: each type is capped at `ceil(k/2)` hits so the others
   backfill from the oversampled tail, and the cap relaxes when there is nothing else to show — it
   diversifies without starving.

Hits come back as whole records, not snippets — a truncated scenario is useless to follow. Returned
records have their `usage_count` incremented.

### Changing the embedding model

Every vector is stamped with the model that produced it. At startup the service re-embeds whatever
the configured model did not write, a bounded slice per boot, so a large table converges over a few
restarts instead of delaying the first one.

This is not bookkeeping. Vectors from two models are not comparable, and one of a different
dimensionality cannot be scored at all — without the stamp, changing `OF_EMBEDDING_MODEL` left every
pre-existing record scoring zero forever, reachable only by an exact title match, with nothing
logged. While the sweep catches up, records still on the old model are skipped by the vector search
(comparing different dimensions raises outright) and stay reachable lexically and by title.

Both retrievers are **optional store capabilities**, `InstructionVectorSearch` and
`InstructionLexicalSearch`, detected with `isinstance`. The composition root probes the database for
`vector` and `pg_textsearch` at startup and builds a store that claims only what the server actually
has — a store that always carried the methods would fail at the first recall instead of at startup.
SQLite and managed Postgres (which cannot set `shared_preload_libraries`) get fewer of them, and
recall degrades rather than breaking. The startup report prints which ones are live.

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
- **A missing search extension costs recall quality, never uptime.**
- **The type filter is applied inside the store query**, not after — `limit` is spent before the
  caller could filter, so a top-N of one type would starve a search for another.
- **Every stored vector knows which model wrote it**, so a model change is a detectable event.
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
| Reranker unavailable | Falls back to the fused (or cosine) ordering |
| `pg_textsearch` absent | Recall is embeddings only; `history_search` stays a substring match |
| `pgvector` absent | The visible table is ranked in process, as it always was |
| Embedding model changed | Old vectors are skipped by vector search and re-embedded over the next few restarts; records stay reachable lexically and by exact title |
| Two records with the same title for one owner | Prevented by the uniqueness constraint; the upsert replaces content and bumps the version |
| Agent tries to edit a system record | `SystemInstructionError`, explained back to the model |
| Overlay file unreadable or malformed | Logged warning, overlay ignored, built-in registry synced as-is |
| Overlay `append` names a record the registry does not have | Logged warning; the rest of the overlay still applies |

## Code anchors

- `core/src/octoforge_core/instructions/api.py` — `Instruction`, `InstructionType`, the store/service
  ports, `InstructionVectorSearch`
- `core/src/octoforge_core/instructions/local.py` — the service: embed, retrieve, fuse, boost, rerank
- `core/src/octoforge_core/instructions/pg_store.py` — the pgvector and BM25 queries
- `core/src/octoforge_core/instructions/ranking.py` — cosine scoring and reciprocal rank fusion
- `core/src/octoforge_core/db/search_extensions.py` — probing and creating the optional extensions
- `core/src/octoforge_core/instructions/ranking.py` — the scoring functions
- `core/src/octoforge_core/instructions/store.py` — SQL store, visibility predicates, uniqueness
- `core/src/octoforge_core/instructions/registry.py` — the declarative system registry and its sync
- `server/src/octoforge_server/skill_overlay.py`, `server/src/octoforge_server/system_skills.py` — the overlay and
  the web-side registry
- `core/src/octoforge_core/instructions/tools.py` — `recall`, `instruction_save`, `instruction_delete`
- `core/tests/test_instructions_local.py`, `core/tests/test_ranking.py`,
  `core/tests/test_instructions_store.py`, `core/tests/test_instructions_registry.py`
