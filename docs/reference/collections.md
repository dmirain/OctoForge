# Collections and task memory

What happens to a large HTTP response instead of truncation. Two tiers, picked by the body's shape:

- an **array of records** becomes a **collection** in the database — its value is queries
  (filter, aggregate, join), and it survives task boundaries;
- a **single document** — one JSON object, an article, any unstructured text — goes to
  **task memory**: RAM, no database, alive exactly as long as the task that fetched it. Its value
  is being *read* (in full, when the model decides its budget affords it) or *searched* (when no
  budget should swallow it whole).

Either way the model receives a **passport** — the shape and the sizes — instead of the first
8000 characters of a body whose tail used to be thrown away.

A collection is **not a table**. One migration created two fixed tables (`collections`,
`collection_records`) once; every collection of every user is rows in them. "Create a collection" is
an INSERT, its schema is a JSON *value* derived from the data, and no DDL ever runs at runtime.

**The database tier is Postgres only.** The query engine compiles the DSL into SQL over jsonb and
has no other implementation; on SQLite `build_collections` answers None and the collection tools do
not exist. **Task memory is universal** — it needs no database, so every installation gets it, and
off Postgres an oversized array degrades into a readable memory document instead of a truncated
stump.

## Task memory

A remembered response gets a `resp:` ref and a passport with the numbers the model needs to decide
what to spend: per-key sizes in characters AND an estimated token cost (Cyrillic tokenizes at
~2.4 chars/token, Latin at ~4 — a plain char budget starves Russian text). It lives until the task
that fetched it terminates (the runner sweeps by task id), under a process-wide LRU budget; a
restart loses it, and the remedy is the remedy for everything here — fetch again.

Three verbs, and deliberately **no sequential window-paging** (reading a megabyte in slices costs
the same tokens as reading it whole, just slower):

| Tool | Behavior |
|---|---|
| `response_get(ref, key?, max_chars?)` | The whole body or one dotted key. The default is conservative; the model raises `max_chars` up to the configured ceiling when the passport's numbers say it fits the budget |
| `response_find(ref, pattern, before?, after?, max_matches?, match_offset?)` | Regex (invalid ones taken literally, case-insensitive) with a window around every match; answers the full match count, merges overlapping windows, and each match carries its `at` position |
| `response_window(ref, at, key?, before?, after?)` | A wider look around a position a find returned — how "the window was too small" is fixed without re-searching |

## How it works

Every path a response body takes — `external_call`, `http_request`, an MCP mirror — hands its
already-scrubbed text to the spill (`ResponseSpill`). The spill answers one of three ways:

- the body is at or under `OF_COLLECTIONS_INLINE_MAX_CHARS` → **inline**, exactly as before;
- a JSON **array** (or CSV) → a **collection**: elements become one row each, a schema is derived
  by folding every record (a field missing somewhere is `optional`, mixed scalar types degrade to
  `string`, sometimes-null keeps its type plus `nullable`), and the tool result is the passport;
- a JSON **single object**, or unstructured/malformed text → **task memory** (see above);
- no user in context, or neither tier available → the old **truncation**.

Unwrapping: a top-level array is the records; an object with exactly one array-of-objects member is
an envelope around its records (the scalar siblings ride into the passport as `envelope`); any other
object is a single record. CSV takes its keys from the header row; values stay strings unless the
endpoint record declares coercions.

The passport names the ref (`col:<id>`), the kind and source, the record count and size, the expiry,
and the rendered record schema — knowing the shape of 1400 records is worth more per token than
seeing one and a half of them.

### The endpoint contract's two new sections

An endpoint record (see [endpoints-and-net.md](endpoints-and-net.md)) may shape its own ingestion —
sections the model writes when authoring the record, reading the API's own documentation:

```json
"response": {"items_path": "data.items",
             "fields": {"id": "number", "name": "string", "amount": "number"}},
"pagination": {"kind": "page", "param": "page", "start": 1, "total_path": "total"}
```

`response.items_path` names where the records live when the unwrap heuristic cannot see it;
`response.fields` is a projection AND a coercion map — only these fields survive into the
collection, and `"12.5"` becomes `12.5` before it is stored, which is what lets `sum(amount)` work.
A value that refuses its coercion stays as it came, and the schema tells the truth about it.

`pagination` is what the **collect loop** walks: `external_call(name, params, collect: true)` fetches
page after page — advancing `param` by page number, item offset, or the cursor read from
`cursor_path` — with **no LLM round-trip inside the loop**, pouring every page into one collection
and answering with its passport. A thousand contractors served fifty at a time is one tool call.
Stop conditions: an empty or unparseable page, a repeated cursor, `total_path` reached, an error
status past the first page, and the page ceiling (`max_pages` argument, capped by
`OF_COLLECTIONS_COLLECT_MAX_PAGES`) — the ceiling marks the collection truncated, because counts
that silently reflect a cap read as the whole truth.

### Appending across endpoints

`external_call(…, into: "col:…")` pours a call's records into an EXISTING collection instead of
creating one — with or without `collect`. Records keep a per-batch `source` tag (the endpoint
name), so a collection can hold contractors from one endpoint and their contacts from another,
queryable apart (`source` filter) or together — the join happens in the database, not in the
context window. An explicit `into` beats the inline threshold: even a small body is stored,
because the caller said where. `label` names a freshly created collection.

### Querying

`collection_query(ref, op, …)` executes a typed DSL **in the database**:

| Op | Answer |
|---|---|
| `get` | whole records, in arrival order, paged |
| `pluck` | one field of every record (dotted paths: `owner.city`) |
| `count` / `sum` / `avg` / `min` / `max` | one number — or one per group with `group_by` |
| `distinct` | the distinct values of a field |

`filters` (`{field, op, value}`; `eq ne gt lt gte lte contains`) narrow every op and combine with
aggregation: "sum of `amount` where `status=active`, grouped by `region`" is one call. `source`
narrows to records that arrived from one endpoint (a collection appended from several). Results
page with `limit`/`offset` and report how many matched in total.

Every field name is validated against the derived schema before compilation; a miss answers with
the fields that do exist, and `sum` over a string field names the remedy. Injection is excluded by
construction: paths travel as bound `text[]` parameters into `#>>`, values as ordinary parameters,
and everything else in the statement comes from fixed vocabularies. The schema picks comparison
semantics — `number` fields cast to numeric, everything else compares as text (which does the right
thing for ISO dates).

`join` pairs every (filtered) record with its matches from a second collection — or the same one,
told apart by `source` — on field equality: `{ref, on_left, on_right, source?}`. It combines with
`get` (rows come back as `{left, right}` pairs) and `count`; both sides' refs are checked for
ownership and expiry, both join fields against their side's schema. An inner join by design: a
record without a match is reachable as plain `get`, and pairing is exactly what was asked for.

`collection_get(ref)` re-reads the passport — the schema reminder after context compaction.

### Lifecycle

Collections are working memory, not storage: each lives `OF_COLLECTIONS_TTL_SECONDS` from its last
touch, a background sweep drops the expired, and per-user quotas (count and bytes) evict the least
recently touched rather than refuse — a fetch must not fail because an old fetch still lingers. An
expired, evicted or foreign ref answers not-found with one remedy: run the call again.

## Invariants

- **No DDL at runtime.** Two fixed tables; a collection is rows.
- **The store sees only scrubbed text**, so a stored body can never resurrect a secret.
- **Owner scoping is a SQL predicate**; a stranger's ref and a nonexistent one are the same answer.
- **A spill failure never fails the call** — the data already arrived; truncation is the fallback.
- **No user, no collection**: a call without a user in context keeps plain truncation.
- **Off Postgres the feature is absent**, not emulated.

## Configuration

| Variable | Default | Effect |
|---|---|---|
| `OF_RESPONSE_MEMORY_MAX_MB` | `8` | Wire ceiling of one remembered response (RAM, not context) |
| `OF_RESPONSE_MEMORY_BUDGET_MB` | `200` | Process-wide LRU budget of task memory |
| `OF_RESPONSE_GET_DEFAULT_CHARS` | `8000` | What `response_get` answers when the model does not choose |
| `OF_RESPONSE_GET_MAX_CHARS` | `100000` | The most one deliberate read may take |
| `OF_COLLECTIONS_INLINE_MAX_CHARS` | `2000` | Bodies at or under this stay inline in the tool result |
| `OF_COLLECTIONS_TTL_SECONDS` | `3600` | How long a collection lives past its last touch |
| `OF_COLLECTIONS_MAX_PER_USER` | `20` | Count quota; over it the oldest is evicted |
| `OF_COLLECTIONS_MAX_MB_PER_USER` | `50` | Byte quota, same eviction |
| `OF_COLLECTIONS_QUERY_MAX_LIMIT` | `500` | The page-size ceiling of `collection_query` |
| `OF_COLLECTIONS_SWEEP_INTERVAL_SECONDS` | `300` | How often expired collections are dropped |

## Failure modes

| Situation | Outcome |
|---|---|
| Ref expired, evicted or someone else's (`col:` or `resp:`) | Not-found text naming the remedy (fetch again) |
| Process restart / dialog migration | Task memory is gone with the process; collections survive |
| Unknown field in a query | Error listing the fields the schema does have |
| `sum`/`avg` over a non-numeric field | Error naming the coercion remedy |
| Body declared JSON but does not parse | Old truncation (the head is more honest) |
| Storage blip during a spill | Logged; the call falls back to truncation |
| SQLite installation | The tools do not exist; truncation everywhere |

## Code anchors

- `core/src/octoforge_core/net/collections/api.py` — DTOs, the DSL, the ports
- `core/src/octoforge_core/net/collections/ingest.py` — the spill, unwrapping, the passport
- `core/src/octoforge_core/net/collections/schema_infer.py` — schema derivation and rendering
- `core/src/octoforge_core/net/collections/engine.py` — DSL → jsonb SQL
- `core/src/octoforge_core/net/collections/store.py` — rows, quotas, TTL
- `core/src/octoforge_core/net/collections/tools.py` — `collection_query`, `collection_get`
- `core/src/octoforge_core/net/response_memory.py` — task memory: the store, the passport, the three verbs
- `core/src/octoforge_core/composition.py` — `build_collections` (Postgres check), `build_response_layer` (always)
