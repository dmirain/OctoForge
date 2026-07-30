# LLM, embedding and reranker clients

The provider-facing layer: one chat client, two embedding backends, two reranker backends, a typed error
taxonomy and a retry policy. Everything above this layer talks to ports and does not know which vendor is
answering.

## The chat client

`LLMClient` (in `core/src/octoforge_core/ports.py`) has two methods:

- `complete(messages, tools?) -> Completion` — one non-streaming answer plus its usage. Used by the router
  and the compactor.
- `stream(messages, tools?) -> AsyncIterator[StreamEvent]` — the streaming path used by every run.

`OpenAICompatibleClient` implements it against any OpenAI-compatible `/chat/completions` endpoint,
including a local Ollama. Streaming is SSE, and the client translates the wire format into typed events:

| Event | Meaning |
|---|---|
| `TextDelta` | An incremental piece of the answer |
| `ToolCallStarted(index, call_id, name)` | A tool-call slot appeared |
| `ToolCallReady(call)` | Its arguments finished streaming and parsed cleanly |
| `ToolCallBroken(index, call_id, name, error, raw)` | Arguments did not parse into a JSON object |
| `StreamFinished(message, usage)` | Terminal: the complete assistant message |
| `RetryScheduled(attempt, delay_seconds, reason)` | The retrying wrapper is about to retry |

A tool call is considered complete when argument deltas move on to the next index (the last one closes at
`finish`). That is what makes eager tool execution possible one layer up — see
[agent-loop.md](agent-loop.md). Broken arguments are reported rather than guessed: no partial-JSON repair.

Mid-stream error payloads are detected and classified like HTTP errors, so a provider that fails halfway
does not look like a truncated answer.

## Errors and retries

Failures are classified into `ErrorKind`: `RATE_LIMIT`, `AUTH`, `QUOTA`, `CONTEXT_OVERFLOW`,
`PROVIDER_INTERNAL`, `TRANSPORT`, `CLIENT`. The distinction is not cosmetic — it decides what happens
next:

- **transient** (`RATE_LIMIT`, `PROVIDER_INTERNAL`, `TRANSPORT`) → retried by `RetryingLLMClient` with
  exponential backoff and full jitter, up to `OF_LLM_MAX_RETRIES`. A provider's `Retry-After` acts as the
  floor of the delay, capped at 300 s so a `Retry-After: 3600` cannot park the process for an hour;
- **`CONTEXT_OVERFLOW`** → surfaces as `ContextOverflowError` and triggers reactive compaction plus one
  retry of the run (see [context-compaction.md](context-compaction.md));
- **everything else** (`AUTH`, `QUOTA`, `CLIENT`) → not retried; the run fails with a readable error.

Streaming calls are retried **only if the failure happened before the first stream event**: once deltas
went downstream, a retry would duplicate partial output.

Every retry is announced as a `RetryScheduled` event, which the loop passes through so a transport can show
"retrying" instead of a frozen cursor.

`retry_transient` is the lighter variant used by the secondary HTTP backends (embeddings, reranker): one
extra attempt with a fixed short delay. A transient 429 should not fail a search, but it must not stall it
either.

## Usage accounting

`Usage(prompt_tokens, completion_tokens, cached_tokens)` is parsed from the provider's response when
present. It rides on `StreamFinished`/`Completion` and is used for the token-based compaction trigger; a
provider that reports nothing simply leaves it `None`.

## Embeddings

`EmbeddingClient` has two shipped backends, chosen by `OF_EMBEDDING_BACKEND`:

| Backend | Implementation | Notes |
|---|---|---|
| `openai` | `llm/embeddings.py` | Any OpenAI-compatible `/embeddings` endpoint. Inherits the LLM's URL and key when nothing embedding-specific is configured |
| `local` | `llm/local_embeddings.py` | In-process sentence-transformers. Requires the `local-embeddings` extra; constructing it without the dependency raises a clear `ImportError` with the install command |

`octoforge-core` never requires torch: importing the package works without the extra, and only
constructing the local backend needs it.

## Reranking

`RerankerClient` is optional and improves `recall` ordering by re-scoring a shortlist:

| Backend | Implementation | Chosen when |
|---|---|---|
| Local cross-encoder | `llm/reranker.py` | `OF_RERANKER_MODEL` is set and no API key is given. CPU-heavy |
| HTTP | `llm/http_reranker.py` | `OF_RERANKER_API_KEY` is set (SiliconFlow-compatible `POST /rerank`) |

With neither configured, ranking is cosine plus the exact-title boost.

## Invariants

- **The core is provider-agnostic.** Nothing above this layer knows which vendor answers.
- **Streaming events are typed**, and a broken tool call is reported, never repaired by guessing.
- **Only transient failures are retried**, and a stream is retried only before its first event.
- **`Retry-After` is honoured as a floor and capped**, so a hostile or careless header cannot stall the
  process.
- **Importing the core never requires torch.**
- **Embedding and reranker failures degrade, they do not break**: a save keeps the record with an empty
  vector, a rerank failure falls back to cosine order.

## Configuration

See [configuration.md](configuration.md) for the full list; the relevant ones are `OF_LLM_*`,
`OF_EMBEDDING_*` and `OF_RERANKER_*`.

## Failure modes

| Situation | Outcome |
|---|---|
| Rate limit or provider 5xx | Retried with backoff; `RetryScheduled` events emitted; then the run fails if it never succeeds |
| Bad key or exhausted quota | Not retried; the run fails with the classified error |
| Context window exceeded | Reactive compaction and one retry |
| Failure after the first delta | Not retried — the partial answer is kept and the run fails |
| Provider reports no usage | Token-based compaction trigger stays inactive; the character threshold still applies |
| `local` backend without the extra installed | `ImportError` naming the install command, at construction time |
| Reranker endpoint down | Cosine ordering is used |

## Code anchors

- `core/src/octoforge_core/ports.py` — the `LLMClient` port
- `core/src/octoforge_core/llm/openai.py` — the OpenAI-compatible client and SSE parsing
- `core/src/octoforge_core/llm/events.py` — the stream event union
- `core/src/octoforge_core/llm/errors.py` — `ErrorKind` and classification
- `core/src/octoforge_core/llm/retry.py` — `RetryingLLMClient`, `retry_transient`
- `core/src/octoforge_core/llm/usage.py` — usage DTOs and parsing
- `core/src/octoforge_core/llm/embeddings.py`, `llm/local_embeddings.py` — embedding backends
- `core/src/octoforge_core/llm/reranker.py`, `llm/http_reranker.py` — reranker backends
- `core/tests/test_openai_client.py`, `core/tests/test_openai_stream.py`, `core/tests/test_llm_errors.py`,
  `core/tests/test_embeddings.py`, `core/tests/test_reranker.py`, `core/tests/test_http_reranker.py`
