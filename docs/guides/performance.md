# Performance

Where latency comes from, what the framework adds, and the rules that keep one asyncio process serving many
users without freezing.

## What the framework costs

`make bench` runs [`tools/bench_latency.py`](../../tools/bench_latency.py): the real stack — manager, actor,
persisted narrative, LLM router, agent loop — against a scripted in-process LLM with known timing. What
remains is ours.

Measured with 15 runs each on an idle 2-vCPU host over SQLite; the medians below are the median of
three separate sessions and the p90 the worst of the three, so a lucky run cannot flatter the table:

| Scenario | Median | p90 | Baseline |
|---|---|---|---|
| `submit()` → the provider is asked | 17 ms | 22 ms | Includes the durable write of the message and its exchange |
| Same, for a message arriving while another answer streams | 13 ms | 17 ms | Includes the routing decision and spawning a second run |
| A token leaving the LLM → the same token at a subscriber | 0.02 ms | 0.07 ms | Deltas are never persisted |
| Three 150 ms tool calls in one assistant message | 151 ms | 151 ms | 449 ms if executed one after another |
| Two questions back to back, 400 ms of answer each | 434 ms | 442 ms | 800 ms if the second waited |

The two write-bound rows roughly halved when SQLite moved to WAL (33 → 17 ms and 26 → 13 ms). That is
attributable rather than assumed: re-running the same harness with the WAL pragma neutralised, on the
same idle host, gives 28 ms and 23 ms. The rest of the gap between those and the older figures is the
host being idle, which the earlier numbers were not measured on. `submit()` is dominated by the
durable write of the message and its exchange, so removing an fsync per commit is exactly where it
shows up; the tool and delivery rows, which write nothing on the measured path, did not move.

For scale: the reasoning model this project is developed against takes ~2.4 s to produce its own first token.
The framework is about 1% of what a user waits for, which is the point — the wins that matter are structural,
not micro-optimizations.

Rerun the harness after touching the actor, the loop or the stores; `--json` gives raw numbers, and the README
quotes the same table.

## The structural wins

**Tools start before the message ends, and run concurrently.** A tool call whose arguments have finished
streaming is executed immediately; results land as they finish. A three-call round costs one call's latency
instead of three. Providers without incremental tool-call events fall back to spawning at the end of the
iteration. See [../reference/agent-loop.md](../reference/agent-loop.md).

**Nothing queues behind anything.** There is no single active run per dialog: obligations stream in parallel.
Cancellation bypasses the actor's inbox, so a stop lands immediately even while a routing call is in flight,
and it races the stream and the running tools rather than waiting for the next token.

**The prompt prefix is byte-stable, so provider caching actually hits.** Prefix caches match token-for-token
from the first divergence, so the design keeps the volatile parts at the tail:

- the system prompt carries no timestamp — the current date and time ride as an envelope on the *last* branch
  message;
- the narrative is append-only, and nothing is edited or reordered;
- the tool list is resolved once per run and does not shuffle between iterations;
- retrieval results are not injected into the system block.

Compaction is the one deliberate exception: replacing a prefix invalidates the cache from that point. It
happens in the background and rarely, so the amortized cost is small — but it is why compaction must not run
every turn.

**The prompt stays small as the dialog grows.** Skills and knowledge are fetched by `recall` when needed
rather than pasted into every request, and compaction keeps history to a rolling summary plus a verbatim hot
tail. Prompt size is roughly flat in dialog age.

**Routing is cheap and often skipped.** One short tool call over one-line descriptions of the live exchanges —
and no call at all for an explicit reply or a dialog with nothing in flight.

## The no-stop-the-world rule

One event loop serves every dialog of a process. Any inline work whose cost grows with data therefore freezes
*all* users, not one.

The measured case that set the rule: pure-Python cosine ranking over 10k instruction records blocked the loop
for ~850 ms on every `recall` — which runs on nearly every message. Vectorizing it with numpy and moving it to
a worker thread was not enough on its own: a single long C call over Python objects (`np.asarray` on tuples)
holds the GIL even from a thread, so the conversion is chunked. Result: maximum event-loop gap 19 ms at 10k
records.

The rules that follow, applied when writing anything in the request path:

1. **If a code path's cost grows with data** (records, history, users) **and can exceed ~10 ms at target
   scale, vectorize it and move it off the loop** with `asyncio.to_thread`.
2. **Mind the GIL.** Chunk long C calls that walk Python objects, or the worker thread stalls the loop anyway.
3. **Never hold a cross-dialog lock across an await.** Runner initialization used to serialize behind one
   global lock, which put every dialog in the process behind the slowest first contact; now the lock guards
   only the build map and each dialog's build is a memoized task.
4. **Latency-critical actions must not queue behind slow work.** Cancellation bypasses the inbox for exactly
   this reason.
5. **Bound every read.** Compaction reads its backlog in windows of 500 rows; dataset and history queries have
   server-side caps.
6. **Keep the transports off the hot path.** Telegram ingestion runs on per-user queues, not inside the poll
   loop, so one slow chat cannot delay everyone's updates.

## Provider-side levers

- **Prompt caching** is free once the prefix is stable (see above); on OpenAI-compatible endpoints it is
  automatic above ~1024 tokens of prefix.
- **A smaller model for routing** is not currently configurable — the router uses the main `LLMClient`. It is
  a cheap call, but on an expensive reasoning model it is not free; swapping in a second client for the router
  is a composition-root change if it matters to you.
- **Streaming drafts** dominate *perceived* latency in messengers. `OF_TELEGRAM_EDIT_THROTTLE_SECONDS` trades
  API calls for smoothness.
- **Embeddings** are called on every `recall`. A local backend removes a network round trip but competes for
  CPU with the event loop; an HTTP backend keeps the CPU free at the cost of latency. On a small host, HTTP is
  usually the better trade.

## The round-trip budget

Latency is one axis; the **number of database round trips** is the other, and it is the one that decides
whether a pod can live away from its database. Measured 2026-08-02 against a copy of a production database
(1220 messages in the dialog, 44 instruction records), LLM and embedder stubbed, everything else real.

One question answered from cold — build the actor, one `recall`, the answer, the closing bookkeeping:

| | Statements | Transactions | Concurrent |
|---|---|---|---|
| Before | 45 | 35 | none |
| After | **33** | **31** | **6** |

Where the twelve went:

- **Seven were the ORM re-reading a row it was about to write.** A SELECT before each UPDATE, inside the
  same store method. They are single UPDATEs now; the condition rides in the WHERE clause and `rowcount`
  answers the question the SELECT was asked ("does this row exist"), so the not-found errors are unchanged.
  A single UPDATE is also atomic on its own — a transaction would have made it correct *and* slower.
- **The claim is one upsert.** `INSERT … ON CONFLICT DO UPDATE … RETURNING generation` replaced a SELECT
  followed by an INSERT-or-UPDATE, and with it the retry loop that existed only for the race in between.
- **The compaction boundary is asked once instead of three times.** The initial narrative load carries it
  as a subquery; prompt assembly takes it from the summaries it already reads.
- **The two recovery sweeps of `tasks` are one query.** Same table, same dialog, disjoint conditions.
- **A freshly created exchange is not re-read** before an owner is assigned to it: its id has not left the
  coroutine that minted it, so nothing can have claimed it. An exchange that already existed still is.

And six of the remaining statements now run **concurrently**: `recall`'s vector search, its two BM25
indexes and the dataset lookup answer independent questions, so none waits out another's round trip. The
usage counter of the hits is written off the answer's path entirely.

What the shape still shows:

- **Every store call is its own transaction.** Nothing groups them, so a request has no atomicity and pays
  BEGIN/COMMIT per call. This is now the dominant cost: removing a statement from inside an existing
  transaction saves one round trip, removing the transaction saves three.
- **Repeats remain** within one turn: the dialog's live exchanges are listed three times, its `updated_at`
  bumped twice, its task re-read twice.

Round trips matter more than milliseconds because they multiply by the distance to the database. On a host
sharing a machine with Postgres a statement costs ~0.3 ms and a session ~0.35 ms; across a WireGuard tunnel
to another datacenter the same numbers were 11.6 ms and 36.5 ms. The same question that costs 77 ms beside
its database costs on the order of a second away from it — which is why **a pod and its database belong in
the same datacenter**, and why the counts above are the thing to drive down.

## What to watch in production

| Symptom | Likely cause |
|---|---|
| Every dialog slows down at once | Something inline whose cost grew with data — check `recall` volume and compaction backlog |
| Answers start slow, tokens then flow fast | Provider TTFT (or a cold prompt cache), not the framework |
| Cancellation feels delayed | A tool with no timeout; the abort only frees the slot after the call returns |
| Runs failing with an idle timeout | Provider stalls; `OF_LLM_STREAM_IDLE_TIMEOUT_SECONDS` is the guard |
| Prompt sizes creeping up | Compaction not keeping up — check its warnings and the hot-tail threshold |

## Code anchors

- `tools/bench_latency.py` — the harness behind the table
- `core/src/octoforge_core/instructions/ranking.py` — the vectorized, chunked, off-loop ranking
- `core/src/octoforge_core/agent/loop.py` — eager tools, cancellation races, the idle watchdog
- `core/src/octoforge_core/agent/runner.py` — the date envelope, per-dialog builds, delivery
- `core/src/octoforge_core/context/compactor.py` — bounded reads, background compaction
- [../AGENTS.md](../../AGENTS.md) — the same rules as coding conventions
