# Collections

What happens to a large structured HTTP response instead of truncation: it becomes a **collection**
the agent queries in the database, and the model receives a **passport** — the shape and the counts —
instead of the first 8000 characters of a body whose tail used to be thrown away.

A collection is **not a table**. One migration created two fixed tables (`collections`,
`collection_records`) once; every collection of every user is rows in them. "Create a collection" is
an INSERT, its schema is a JSON *value* derived from the data, and no DDL ever runs at runtime.

**Postgres only.** The query engine compiles the DSL into SQL over jsonb and has no other
implementation; on SQLite the feature is simply not wired (the composition root's
`build_collections` answers None), the tools do not exist, and responses keep the old truncation.

## How it works

Every path a response body takes — `external_call`, `http_request`, an MCP mirror — hands its
already-scrubbed text to the spill (`ResponseSpill`). The spill answers one of three ways:

- the body is at or under `OF_COLLECTIONS_INLINE_MAX_CHARS` → **inline**, exactly as before;
- the body parses as JSON or CSV → a **collection**: elements become one row each, a schema is
  derived by folding every record (a field missing somewhere is `optional`, mixed scalar types
  degrade to `string`, sometimes-null keeps its type plus `nullable`), and the tool result is the
  passport;
- anything else (unstructured, malformed, no user in context) → the old **truncation**.

Unwrapping: a top-level array is the records; an object with exactly one array-of-objects member is
an envelope around its records (the scalar siblings ride into the passport as `envelope`); any other
object is a single record. CSV takes its keys from the header row and its values stay strings until
an endpoint contract declares coercions (planned: the `response` section).

The passport names the ref (`col:<id>`), the kind and source, the record count and size, the expiry,
and the rendered record schema — knowing the shape of 1400 records is worth more per token than
seeing one and a half of them.

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
| `OF_COLLECTIONS_INLINE_MAX_CHARS` | `2000` | Bodies at or under this stay inline in the tool result |
| `OF_COLLECTIONS_TTL_SECONDS` | `3600` | How long a collection lives past its last touch |
| `OF_COLLECTIONS_MAX_PER_USER` | `20` | Count quota; over it the oldest is evicted |
| `OF_COLLECTIONS_MAX_MB_PER_USER` | `50` | Byte quota, same eviction |
| `OF_COLLECTIONS_QUERY_MAX_LIMIT` | `500` | The page-size ceiling of `collection_query` |
| `OF_COLLECTIONS_SWEEP_INTERVAL_SECONDS` | `300` | How often expired collections are dropped |

## Failure modes

| Situation | Outcome |
|---|---|
| Ref expired, evicted or someone else's | Not-found text naming the remedy (fetch again) |
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
- `core/src/octoforge_core/composition.py` — `build_collections`, the Postgres capability check
