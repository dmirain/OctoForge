# Datasets

Structured per-user data the agent can write and query: expense lines, reading lists, measurements,
whatever a user asks it to keep. A dataset has a declared schema, so what goes in stays queryable.

## How it works

A dataset belongs to one owner and carries a name, a human description and a schema. Its records are
JSON payloads validated against that schema on write.

The schema document is intentionally small:

```json
{"fields": [
  {"name": "item", "type": "string", "required": true},
  {"name": "amount", "type": "number"},
  {"name": "spent_on", "type": "date"}
]}
```

Field types are `string`, `integer`, `number`, `boolean`, `date`, `datetime`; `required` defaults to
false. A malformed schema is rejected at creation time (`DatasetSchemaError`) with a readable reason —
not an object, missing or invalid fields, duplicate names, unknown type. A record that violates the
schema is rejected with `DatasetRecordValidationError`, which the agent reads and can correct.

### Tools

| Tool | Behavior |
|---|---|
| `data_put` | Create a dataset (name, description, schema) and/or append validated records to it |
| `data_query` | Read records back with filters, up to `OF_DATASETS_QUERY_MAX_LIMIT` |
| `data_forget` | Delete records, or a whole dataset |

Dataset **descriptors** (name plus description) are embedded and take part in `recall`, which is how the
agent finds out that a relevant dataset exists at all before querying it. Records themselves are not
embedded — they are queried, not searched by meaning.

### Isolation

Ownership is enforced in SQL, not in the tool layer: every query is scoped by the owner id taken from
the session. Two users may keep datasets with the same name and never see each other's; a request for a
dataset that belongs to somebody else is indistinguishable from a request for one that does not exist.

Like the instruction store, a dataset store may implement the runtime-checkable `DatasetVectorSearch`
capability, in which case the service stops pulling descriptors into the process for ranking.

## Invariants

- **Owner scoping is a SQL predicate**, applied to every read and write.
- **A record is validated before it is stored.** There is no "store now, fix later" path.
- **The schema is fixed at creation.** Changing shape means a new dataset (or deleting and recreating),
  which keeps stored records consistent with the schema they were validated against.
- **Dataset names are unique per owner** (`DatasetExistsError` otherwise).
- **Descriptors are searchable, records are not** — deliberately: dumping record contents into `recall`
  results would flood the context.
- **Query limits are enforced server-side** — the agent cannot ask for an unbounded page.
- **Validation errors go back to the model as text**, so the next attempt can fix the payload.

## Configuration

| Variable | Effect |
|---|---|
| `OF_DATASETS_QUERY_DEFAULT_LIMIT` | Page size when the agent does not specify one (default 50) |
| `OF_DATASETS_QUERY_MAX_LIMIT` | Hard cap per query (default 200) |
| `OF_EMBEDDING_*` | Needed for descriptors to be searchable; without embeddings dataset search is unavailable |

## Failure modes

| Situation | Outcome |
|---|---|
| Schema malformed at creation | `DatasetSchemaError` with the reason; nothing is stored |
| Record violates the schema | `DatasetRecordValidationError`; the agent can correct and retry |
| Dataset name already used by this owner | `DatasetExistsError` |
| Dataset belongs to another user | Looks like "not found" |
| No embeddings backend | Datasets still work through explicit names; they just do not surface in `recall` |
| Query asks for more than the cap | Clamped to the maximum |

## Code anchors

- `core/src/octoforge_core/datasets/api.py` — DTOs, ports, errors, `FieldType`
- `core/src/octoforge_core/datasets/validation.py` — schema parsing and record validation
- `core/src/octoforge_core/datasets/service.py` — orchestration (embed descriptor, validate, store)
- `core/src/octoforge_core/datasets/store.py` — SQL store with owner scoping
- `core/src/octoforge_core/datasets/tools.py` — `data_put`, `data_query`, `data_forget`
- `core/tests/test_datasets.py`, `core/tests/test_dataset_validation.py`, `core/tests/test_data_tools.py`
