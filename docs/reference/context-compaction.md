# Context and compaction

A dialog that runs for months cannot send its whole history to the model on every turn. Compaction
keeps the prompt bounded by replacing the older part with a rolling summary while the recent part stays
verbatim — and keeps the discarded detail reachable through search.

## How it works

There are three levels of context, and each is derived from data rather than tracked by a pointer:

| Level | Where | Contents |
|---|---|---|
| Archive | `messages` table (written by the actor) | Every message ever persisted, with a per-dialog `seq` |
| Topics | `dialog_summaries` table (written by the compactor) | One rolling summary with topic tags and the `[seq_from, seq_to]` range it covers |
| Hot tail | Archive messages with `seq > max(seq_to)` | The verbatim recent history |

Because the boundary is `max(seq_to)` of the summaries, there is no "compacted up to here" pointer to
keep consistent: the state survives any restart and cannot drift.

### Assembling a branch

`ContextCompactor.assemble(dialog, history)` returns `[topics block?] + hot tail`: one system message
carrying the rolling summary (omitted when there is none), then the tail taken from the actor's
in-memory narrative. It also returns `tail_count` and `snapshot_len`, which the actor uses to trim its
in-memory narrative down to the hot tail and to shift process watermarks safely — both computed from
the snapshot taken inside `assemble`, never from the live list length, so a message appended during
the assemble's awaits is neither trimmed nor counted as seen.

### Compaction itself

When the tail outgrows `OF_CONTEXT_HOT_MAX_CHARS` (characters, a ~4:1 proxy for tokens), a background
asyncio task compresses the oldest tail messages into the single rolling summary: one LLM call, one
store write, at most `OF_CONTEXT_COMPACT_TARGET_CHARS` of material per run. The fresh tail is never
touched.

Compaction is technical work, not a dialog process: it is invisible to the user, guarded to one run per
dialog, and a failure is a logged warning rather than a dialog error. A dialog with a long backlog is
read in bounded windows (500 rows) so one run cannot pull an entire history into memory.

### Reactive compaction

If the provider rejects a request for exceeding its context window, that is caught as
`ContextOverflowError`: the actor runs `compact_now()` synchronously, rebuilds the branch, and retries
the run once. A second overflow fails the run rather than looping.

A token-based trigger exists for providers that report usage: set `OF_MODEL_CONTEXT_TOKENS` to the
model's window and compaction starts when reported usage approaches it minus
`OF_CONTEXT_BUFFER_TOKENS`. With `0` (the default) only the character threshold applies.

### Searching what was compacted

`history_search` gives the agent the archive back: a text query over the dialog's own messages, with
optional filters by topic (resolved to the seq ranges of matching summaries) and by date. The topics
block in the prompt is what tells the model such history exists and what it is about — which is why the
summary carries topic tags rather than being free prose.

How the query matches depends on the database. With `pg_textsearch` it is a **BM25 search**: the query
is stemmed through the same `russian_unaccent` configuration the rest of retrieval uses, and hits come
back by relevance. On SQLite it is **FTS5**, also BM25-ranked but tokenized by trigram, so it matches
substrings instead of stems. With neither, the fallback is a **substring match**
(`content ILIKE '%query%'`) ordered by position in the dialog. The difference is not cosmetic in an inflected language — a
substring search for "задача" does not find "задачи", so the tool only works when the user happens to
type the exact form somebody wrote months earlier.

Compaction is a one-time cost to provider prompt caching: replacing a prefix invalidates the cache from
that point. It happens rarely and in the background, so the amortized effect is small — see
[../guides/performance.md](../guides/performance.md).

## Invariants

- **The compaction boundary is derived from the summaries**, never stored separately.
- **The hot tail is verbatim.** Nothing inside it is rewritten, so the recent conversation is always
  exact.
- **The narrative stays append-only.** Compaction adds a summary and lets the actor trim its in-memory
  mirror; it never edits messages in place.
- **Trimming and watermarks use the compactor's snapshot length**, which keeps concurrent appends safe.
- **One compaction run per dialog at a time.**
- **A compaction failure never surfaces to the user** — it is a warning; the dialog keeps working with
  a longer tail.
- **An interrupted assistant turn is marked** (`INTERRUPTED_NOTE`) so a later run knows the text may be
  incomplete.
- **The compactor is a port.** `NoopContextCompactor` (pass-through, no compaction) is a valid choice
  for an embedder that manages history itself.

## Configuration

| Variable | Effect |
|---|---|
| `OF_CONTEXT_HOT_MAX_CHARS` | Tail size that triggers background compaction (default 12000) |
| `OF_CONTEXT_COMPACT_TARGET_CHARS` | Target size of one compressed segment (default 6000) |
| `OF_MODEL_CONTEXT_TOKENS` | Model window for the usage-based trigger; `0` disables it |
| `OF_CONTEXT_BUFFER_TOKENS` | Margin subtracted from that window (default 2000) |
| `OF_HISTORY_SEARCH_DEFAULT_LIMIT` / `OF_HISTORY_SEARCH_MAX_LIMIT` | `history_search` result limits |

## Failure modes

| Situation | Outcome |
|---|---|
| Summarization LLM call fails | Warning logged; the tail keeps growing; retried on the next trigger |
| Provider context overflow | `compact_now()`, branch rebuilt, run retried once; a second overflow fails the run |
| Compaction disabled (`NoopContextCompactor`) | Prompts grow with the dialog; the provider's window becomes the limit |
| `history_search` with a topic that matches no summary | Returns "no hits" rather than searching everything |
| No lexical engine at all | `history_search` falls back to a substring match with no stemming and no relevance ordering |
| Long-uncompacted backlog | Compaction advances in bounded windows instead of reading everything at once |

## Code anchors

- `core/src/octoforge_core/context/api.py` — `ContextCompactor`, `SummaryStore`, `MessageArchive`,
  `AssembledContext`, `DialogueSummary`
- `core/src/octoforge_core/context/compactor.py` — `LlmContextCompactor`, the triggers, the merge
- `core/src/octoforge_core/context/prompts.py` — the summarization prompt and reply parsing
- `core/src/octoforge_core/context/store.py` — summaries and archive search
- `core/src/octoforge_core/context/tools.py` — the `history_search` tool
- `core/src/octoforge_core/context/pg_store.py` — the BM25-ranked archive search
- `core/src/octoforge_core/context/sqlite_store.py` — the FTS5 equivalent
- `core/src/octoforge_core/db/sqlite_fts.py` — the FTS5 mirrors, triggers and query escaping
- `core/tests/test_context_compactor.py`, `core/tests/test_context_integration.py` — behavior
