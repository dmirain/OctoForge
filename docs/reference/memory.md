# Memory

What the agent remembers about one person: birthdays, relatives, preferences, working habits. A memory
follows its user across every surface and is never shared.

## How it works

Memory is not a separate store. A memory is a record in the instruction store with
`InstructionType.MEMORY`, whose **title is the memory key** and whose owner is the user. It therefore
shares the table, the embeddings and the ranking machinery with skills and knowledge — which is the whole
point: one search (`recall`) covers what the agent knows *and* what it knows about you.

Two intent-shaped tools remain:

| Tool | Behavior |
|---|---|
| `memory_store(key, content, tags?)` | Upsert — an existing key is replaced, and the record's version is bumped |
| `memory_delete(key)` | Remove the caller's memory |

There is no `memory_search`. Reading happens through `recall`, optionally narrowed with
`type=memory`.

The tool descriptions draw the line for the model: personal facts about the user go to
`memory_store`; facts useful to everyone go to `instruction_save` as knowledge.

### Visibility

A memory is always owned and never publishable: `publish` on a memory record answers "not found", so an
admin cannot accidentally make one public. The operator console lists memories in their own shape (key,
owner) rather than as raw instruction records, and cross-user instruction search hides them unless they
are asked for explicitly.

### Storage history

Memories used to live in a `memories` table. Migration `f2a6c8d1e935` folded them into the instruction
store; the `Memory` DTO survives only as the read model the console renders.

## Invariants

- **A memory always has an owner.** Nothing creates a global memory (legacy rows with no owner may exist
  from before the merge and are treated as such by the read model).
- **Memories are never published.**
- **The key is the identity.** Storing the same key again replaces the content instead of accumulating
  duplicates.
- **Saving is lenient about embeddings**: if the embedding call fails the record is stored with an empty
  vector and re-embedded by the startup sweep — a remembered fact is never lost because a model endpoint
  blinked.
- **Reading goes through the shared search**, so memory ranking behaves exactly like the rest of the
  store.

## Configuration

| Variable | Effect |
|---|---|
| `OF_EMBEDDING_*` | Memories are searchable only with a working embeddings backend |
| `OF_INSTRUCTIONS_TOP_K` | How many records (memories included) `recall` returns by default |

## Failure modes

| Situation | Outcome |
|---|---|
| Embedding backend down while storing | Memory stored without a vector; re-embedded at the next startup |
| No embeddings backend at all | `memory_store` is unavailable, like every instruction write |
| Agent tries to publish a memory | Reported as not found |
| Deleting a key that does not exist | Reported to the model as such; no error escapes the run |

## Code anchors

- `core/src/octoforge_core/memory/api.py` — the `Memory` read-model DTO
- `core/src/octoforge_core/memory/tools.py` — `memory_store`, `memory_delete`
- `core/src/octoforge_core/instructions/api.py` — `InstructionType.MEMORY` and its rules
- `core/src/octoforge_core/db/migrations/versions/` — `f2a6c8d1e935`, the fold-in migration
- `core/tests/test_memory_tools.py`
