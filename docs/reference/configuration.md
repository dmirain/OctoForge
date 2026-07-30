# Configuration

Every setting is an environment variable prefixed `OF_`, read once at startup into a pydantic
`Settings` object; a `.env` file in the working directory is loaded automatically. Defaults below are
the ones in the code, not suggestions.

## How it works

`web/src/octoforge_web/config.py` defines the settings and the derived questions the composition root
asks them (`embeddings_configured()`, `vision_configured()`, `speech_configured()`,
`embeddings_inherit_llm()`, …). `web/src/octoforge_web/main.py:runtime()` reads those answers to
decide which ports get real implementations and which stay `None`.

An empty value is a real answer: it means the capability is off. Nothing degrades into a stub, and
nothing guesses. What the current configuration adds up to is logged at startup by
`web/src/octoforge_web/capabilities.py` — one line per capability with the endpoint or model behind
it, and a warning for the two gaps that make an installation useless or unreachable (embeddings and
the operator credential). No secret value is ever logged.

Two settings inherit from another when left alone, because one gateway usually serves several
endpoint kinds:

- **Embeddings** inherit `OF_LLM_BASE_URL` and `OF_LLM_API_KEY` when the backend is the HTTP one,
  `OF_EMBEDDING_API_KEY` is empty and `OF_EMBEDDING_BASE_URL` is untouched. Setting either turns
  inheritance off. `OF_EMBEDDING_MODEL` still has to name a model that endpoint serves.
- **Vision** inherits `OF_LLM_BASE_URL` and `OF_LLM_API_KEY` when `OF_VISION_BASE_URL` /
  `OF_VISION_API_KEY` are empty.

Speech deliberately does **not** inherit: `/audio/transcriptions` is a different endpoint kind and a
chat-only gateway answers 404, so both `OF_STT_BASE_URL` and `OF_STT_MODEL` must be set explicitly.

## Retention

How long each kind of row is kept. **Every one defaults to forever, and nothing is deleted until an
operator sets a limit** — a retention policy switched on by an upgrade would destroy data the
installation believed it had.

| Variable | Effect |
|---|---|
| `OF_RETENTION_MESSAGES_DAYS` | Age at which a message may be pruned; 0 = keep forever |
| `OF_RETENTION_EXCHANGES_DAYS` | Age at which a settled exchange may be pruned; 0 = keep forever |
| `OF_RETENTION_TASKS_DAYS` | Age at which a delivered task may be pruned; 0 = keep forever |

Only transcript-shaped data ages out. Instructions, datasets and their records are things a user
wrote on purpose — a skill, a memory, a food diary — and retention never touches them.

The sweep runs once at startup and refuses to delete three things regardless of age: a message at or
after its dialog's compaction boundary (those are what a restarting runner reloads as its narrative),
an exchange that is still live, and a task whose result has not reached the user. Age-based rather
than count-based, so a quiet week never empties a dialog.

The startup capability report prints the policy, so "is anything being deleted here" is answerable at
a glance.

## The model

| Variable | Default | Meaning |
|---|---|---|
| `OF_LLM_BASE_URL` | `https://api.openai.com/v1` | Any OpenAI-compatible endpoint, including a local Ollama |
| `OF_LLM_API_KEY` | *(empty)* | Without it nothing can be generated |
| `OF_LLM_MODEL` | `gpt-4o-mini` | Model id for answers and for the router |
| `OF_AGENT_MAX_ITERATIONS` | `10` | Iterations (model call + tool round) per run; a runaway backstop, not a target |
| `OF_LLM_STREAM_IDLE_TIMEOUT_SECONDS` | `120.0` | Longest silence between stream events before the run fails; `0` disables |
| `OF_LLM_MAX_RETRIES` | `3` | Attempts on transient provider failures (rate limit, 5xx, transport) |
| `OF_LLM_RETRY_BASE_SECONDS` | `1.0` | Backoff base |
| `OF_LLM_RETRY_MAX_SECONDS` | `30.0` | Backoff ceiling |

## Embeddings and reranking

Without a working embeddings backend the application still starts, but `recall`, instruction saving
and dataset search are unavailable — the system-record sync is skipped too.

| Variable | Default | Meaning |
|---|---|---|
| `OF_EMBEDDING_BACKEND` | `openai` | `openai` (HTTP) or `local` (in-process sentence-transformers, needs the `local-embeddings` extra) |
| `OF_EMBEDDING_BASE_URL` | `https://api.openai.com/v1` | HTTP backend endpoint; untouched means "inherit the LLM's" |
| `OF_EMBEDDING_API_KEY` | *(empty)* | Empty means "inherit the LLM's key" |
| `OF_EMBEDDING_MODEL` | `text-embedding-3-small` | Model id (HTTP) or Hugging Face model name (local) |
| `OF_EMBEDDING_BATCH_SIZE` | `16` | Batch size of the local backend |
| `OF_INSTRUCTIONS_TOP_K` | `5` | Default number of records `recall` returns |
| `OF_RERANKER_MODEL` | *(empty)* | Cross-encoder model; empty means ranking is cosine plus title boost only |
| `OF_RERANKER_CANDIDATES` | `20` | Shortlist size handed to the reranker |
| `OF_RERANKER_API_KEY` | *(empty)* | Set to use the HTTP reranker instead of the local cross-encoder |
| `OF_RERANKER_API_URL` | `https://api.siliconflow.cn/v1/rerank` | HTTP reranker endpoint |
| `OF_RERANKER_TIMEOUT_SECONDS` | `30.0` | Request timeout of the HTTP reranker |

## Storage

| Variable | Default | Meaning |
|---|---|---|
| `OF_DATABASE_URL` | `sqlite+aiosqlite:///./octoforge.db` | Async SQLAlchemy URL. Postgres (`postgresql+asyncpg://`) for deployment; SQLite has exactly one writer, so one process only |
| `OF_TELEGRAM_DATABASE_URL` | `sqlite+aiosqlite:///./telegram.db` | Separate database of the Telegram invite/member store |

## Dialog behavior

| Variable | Default | Meaning |
|---|---|---|
| `OF_MAX_PROCESSES` | `5` | Concurrent processes (and therefore live exchanges) per dialog |
| `OF_ROUTER_TIMEOUT_SECONDS` | `10.0` | Router LLM call timeout; on timeout the message opens a new exchange |
| `OF_MATERIAL_QUIET_SECONDS` | `30.0` | How long forwarded material may stay quiet before the agent reacts on its own |
| `OF_MATERIAL_SWEEP_INTERVAL_SECONDS` | `10.0` | How often the sweep looks for settled collections |
| `OF_CONTEXT_HOT_MAX_CHARS` | `12000` | Hot-tail size that triggers background compaction (characters, ~4:1 proxy for tokens) |
| `OF_CONTEXT_COMPACT_TARGET_CHARS` | `6000` | Target size of one compressed segment |
| `OF_MODEL_CONTEXT_TOKENS` | `0` | Model context window for the token-based trigger; `0` disables it |
| `OF_CONTEXT_BUFFER_TOKENS` | `2000` | Safety margin subtracted from that window |
| `OF_HISTORY_SEARCH_DEFAULT_LIMIT` | `20` | Default `history_search` result count |
| `OF_HISTORY_SEARCH_MAX_LIMIT` | `100` | Maximum it will return |

## Scheduling

| Variable | Default | Meaning |
|---|---|---|
| `OF_CRON_POLL_INTERVAL_SECONDS` | `1.0` | How often the scheduler looks for due jobs |
| `OF_CRON_LEASE_TTL_SECONDS` | `60.0` | Claim lease; lets several instances run schedulers safely |
| `OF_CRON_REPLAY_LIMIT` | `5` | Missed firings coalesced per tick |

## Tools and outbound calls

| Variable | Default | Meaning |
|---|---|---|
| `OF_SERPER_TOKEN` | *(empty)* | serper.dev token; empty hides the `web_search` tool |
| `OF_HTTP_REQUEST_ALLOWLIST` | *(empty)* | Comma-separated origins `http_request` may call. Empty means the open web; a list confines the agent's raw HTTP to named destinations, which closes the prompt-injection exfiltration channel |
| `OF_SELF_BASE_URL` | `http://127.0.0.1:8000` | This application's own API as the agent sees it; allowlisted in the SSRF guard so stored endpoints can target it |
| `OF_EXTERNAL_CALL_AUTH_WHITELIST` | `[]` | JSON list of `{base_url_prefix, header_name, header_value}`: infrastructure auth injected by `external_call` for matching prefixes |
| `OF_DATASETS_QUERY_DEFAULT_LIMIT` | `50` | Default `data_query` page size |
| `OF_DATASETS_QUERY_MAX_LIMIT` | `200` | Maximum it will return |

## Secrets

| Variable | Default | Meaning |
|---|---|---|
| `OF_SECRETS_KEY` | *(empty)* | Fernet master key of the per-user secret store; empty disables the feature and endpoints declaring `auth.secret` fail with a clear error |
| `OF_PUBLIC_BASE_URL` | *(falls back to `OF_SELF_BASE_URL`)* | Origin used to build the one-time secret-entry links users receive |

Generate a key with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`.
Losing it makes every stored secret unreadable; it is not derived from anything else.

## Prompts and system records

| Variable | Default | Meaning |
|---|---|---|
| `OF_SYSTEM_PROMPT_SOURCE` | *(empty)* | `file:/path` override of the system prompt, re-read every turn |
| `OF_ROUTER_PROMPT_SOURCE` | *(empty)* | `file:/path` override of the router prompt (template with `{limit}`/`{exchanges}`) |
| `OF_SYSTEM_SKILLS_SOURCE` | *(empty)* | `file:/path` JSON overlay applied to the built-in system records before the startup sync |

Only the `file:` scheme is supported: any other value raises at startup rather than silently falling back.
A prompt file that cannot be read falls back to the built-in prompt, and an unreadable or malformed skill
overlay is a logged warning — a broken customization must never take an installation down.

## HTTP surface

| Variable | Default | Meaning |
|---|---|---|
| `OF_ADMIN_USERNAME` | `admin` | Operator credential for the whole HTTP surface |
| `OF_ADMIN_PASSWORD_HASH` | *(empty)* | PBKDF2 hash (`pbkdf2_sha256:iterations:salt:digest`). **Empty means every request answers 503** — it fails closed. Generate with `tools/hash_password.py`, or let `make quickstart` do it |

The hash uses `:` separators rather than `$` because docker compose interpolates `$` in `.env`.

## Images and voice

| Variable | Default | Meaning |
|---|---|---|
| `OF_VISION_MODEL` | `minimax-m3` | Model that describes incoming images; empty turns ingestion off and images arrive as placeholders |
| `OF_VISION_DEEP_MODEL` | `qwen3.5:397b` | Stronger tier used only by the `image_look` tool; empty hides the tool |
| `OF_VISION_BASE_URL` | *(inherits `OF_LLM_BASE_URL`)* | Vision endpoint |
| `OF_VISION_API_KEY` | *(inherits `OF_LLM_API_KEY`)* | Vision key |
| `OF_STT_BASE_URL` | *(empty)* | Transcription endpoint; no fallback to the LLM's |
| `OF_STT_MODEL` | *(empty)* | Transcription model; both this and the URL are required |
| `OF_STT_API_KEY` | *(empty)* | Transcription key |
| `OF_STT_LANGUAGE` | *(empty)* | Language hint; empty leaves autodetection on |
| `OF_VOICE_MAX_SECONDS` | `600.0` | Longest recording accepted, a guard on latency and provider quota |

## Telegram

| Variable | Default | Meaning |
|---|---|---|
| `OF_TELEGRAM_BOT_TOKEN` | *(empty)* | Bot token from @BotFather; empty means the adapter does not start |
| `OF_TELEGRAM_ADMIN_IDS` | *(empty)* | Comma-separated numeric ids. Admins bypass the invite gate and get the `admin_manage` tool. **While this list is empty the invite gate is inactive and the bot answers everyone** |
| `OF_TELEGRAM_BOT_USERNAME` | *(empty)* | The bot's public handle; accepts `name`, `@name` or a t.me URL. Set it and `admin_manage` hands out invites as `https://t.me/<bot>?start=<code>` deep links instead of bare codes |
| `OF_TELEGRAM_INVITE_TTL_SECONDS` | `259200.0` | How long a generated invite code stays claimable (3 days) |
| `OF_TELEGRAM_POLL_TIMEOUT_SECONDS` | `30.0` | Long-poll timeout |
| `OF_TELEGRAM_EDIT_THROTTLE_SECONDS` | `1.5` | Minimum interval between edits of a streaming draft message |
| `OF_TELEGRAM_RICH_MESSAGES` | `true` | Upgrade finals containing tables, checklists, `<details>` or math to native Rich Messages |

In the local quickstart stack the bot is opt-in through `OF_QUICKSTART_TELEGRAM_TOKEN` instead — a
bot can only be long-polled by one process, and a second poller would steal a live bot's updates.
That variable is read by `docker-compose.local.yml`, not by the application.

## Invariants

- Settings are read once at startup. Changing a variable requires a restart, with two exceptions:
  prompt files (`file:` sources) are re-read on every turn, and the system-record overlay is applied
  at each startup.
- An empty optional setting always means "off", never "default endpoint" or "best effort".
- The capability report reflects the same predicates the composition root uses, so it cannot claim a
  feature the graph does not have.

## Failure modes

| Situation | What happens |
|---|---|
| No `OF_LLM_API_KEY` | The app starts; every run fails at the provider call. The report warns |
| No embeddings backend | The app starts, system-record sync is skipped, `recall`/save/dataset search are unavailable. The report warns |
| Empty `OF_ADMIN_PASSWORD_HASH` | Every HTTP request except `/health`, `/health/ready` and the token-authenticated secret form answers 503 |
| Malformed `OF_SECRETS_KEY` | Startup fails immediately rather than surfacing later as a confusing per-call error |
| `file:` prompt source pointing nowhere | Startup fails fast |
| Overlay file missing or malformed | A logged warning; the built-in system registry keeps serving |
| A `$` inside a value in `.env` under docker compose | The value arrives mangled in the container; escape it as `$$` |
| SQLite URL with more than one process | Writes collide — SQLite allows exactly one writer |

## Code anchors

- `web/src/octoforge_web/config.py` — the settings, defaults and derived predicates
- `web/src/octoforge_web/capabilities.py` — the startup report
- `web/src/octoforge_web/main.py` — where each setting turns into a port or a `None`
- `.env.example` — the annotated variable list
- `web/tests/test_config.py`, `web/tests/test_capabilities.py` — behavior of the above
