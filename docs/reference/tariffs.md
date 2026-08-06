# Tariffs and usage

Per-user plans: which features a user may use and how much they may spend per day. The mechanism is
data-driven end to end — zero tariff rows leave everything unlimited and unmetered checks all pass, so
an installation that never opens the tariffs tab behaves exactly as before. A user bound to no tariff
falls back to the **default plan** if the operator marked one (the «дефолт» checkbox; at most one
plan carries the flag) — with no default marked they are unrestricted, the deliberate behavior of a
private installation. A public installation should mark its freemium plan default: without it, every
new user starts unlimited.

## How it works

Three tables (see [data-model.md](data-model.md)):

- `tariffs` — the operator-defined catalog: a set of `FeatureCode`s plus nullable numeric caps
  (`NULL` = unlimited in that dimension). At most one plan is marked default; the store demotes the
  previous default whenever a put marks a new one.
- `user_tariffs` — at most one binding per user; no row = the default plan, or no restrictions when
  none is marked. An explicit binding always wins over the default.
- `usage_events` — an **insert-only ledger** of metered actions. A log, not a counter: every event
  carries its kind, its origin (`interactive` / `cron` / `background`) and the ids of the entities it
  belongs to (`dialog_id`, `exchange_id`, `task_id` — nullable, because routing happens before any
  task exists and a user message belongs to none). Any window — the UTC day the limits use, a month
  for a report — is a sum over the ledger through the `(user_id, created_at)` index, and concurrent
  writers on two nodes never contend.

### What is metered

| Event (`kind`) | Where | Measures |
|---|---|---|
| `llm_answer` | one per finished run (`ConversationRunner._finalize`) | tokens of **every** iteration (the terminal event alone carries only the last one), `quantity=1` for a visible final; failed and cancelled runs still ledger their tokens |
| `llm_routing` | the runner, from `RouteDecision.usage` — the router itself does not know whose message it routes | routing-call tokens |
| `llm_compaction` | the compactor, on the dialog's user | summarization tokens |
| `user_message` | every persisted submit (duplicates are not counted) | `quantity=1` |
| `voice_transcription` | the Telegram poller, after a successful transcription | seconds of audio |
| `vision` | the Telegram poller after ingest describes (only what was delivered), and `image_look` after a strong-tier call | images |

The public MCP skill-generation call (`mcp/skills.py`) has no user to attribute and is not metered.
Metering runs outside any unit of work, after the persist commits, and a metering failure never fails
the run — the event is logged as lost instead.

### What a tariff limits

- **Daily budgets** (UTC calendar day): `daily_tokens` (prompt + completion of every attributable
  call), `daily_user_messages`, `daily_assistant_messages`. They are enforced at **one choke point —
  the start of an LLM run** (`LimitGate.check_run_budget`, all three budgets together). Intake never
  refuses: a message is always persisted and counted, only the run is withheld — an answer start
  delivers a broker notice (one per dialog per day) and leaves the exchange OPEN, so the next
  trigger after the budget resets picks the work up; a cron wake is skipped (`WakeOutcome.LIMITED`,
  one notice per job per day; the lease retries silently); an agent-spawned task is refused with
  text; a restart-orphaned task is failed with the reason. A run already in flight is never killed.
- **Feature switches**: plain string codes end to end. The core ships its vocabulary as
  `FeatureCode` / `CORE_FEATURES` (`skill_create`, `voice_transcription`, `web_search`, `mcp_add`,
  `http_endpoints` = `http_request` + `external_call`, `vision`), and an assembly extends it without
  touching the core (below). The runner resolves the user's feature set once per run into
  `ToolContext.enabled_features`; each gated tool hides itself via the registry's `visible_to` hook
  and re-checks inside `execute`. Skill saves are gated per *kind*: the same tool still saves
  knowledge on every plan. Telegram checks `voice_transcription` and `vision` before downloading a
  byte, resolving the core person first — plans are never filed under a `tg:` handle; a denied
  picture or recording still delivers its caption as text, and a denied forward keeps its
  material path.
- **Count caps**: `max_cron_jobs` (enforced by `cron.api.create_job` and, through the shared
  `job_quota_refusal`, by `POST /api/cron/jobs` — HTTP 403) and `max_datasets` (enforced by the
  dataset service, so the agent's implicit create on a first `data_put` goes through the same gate).

### Gating a custom tool — no core changes

A feature is data, not an enum case. To ship a gated tool in your own assembly:

1. declare the code next to the tool: `MY_TOOL_FEATURE = "my_tool"` (same `[a-z0-9_]{1,64}`
   grammar as every other code);
2. give the tool `visible_to(context)` returning
   `feature_enabled(context.enabled_features, MY_TOOL_FEATURE)` and re-check inside `execute`,
   answering `feature_refusal(MY_TOOL_FEATURE)` — the same defence-in-depth shape the core tools
   use;
3. in your composition root, register the tool and pass
   `known_features=CORE_FEATURES | {MY_TOOL_FEATURE}` into `Runtime` — that set is the console's
   form vocabulary and its validation list, so the operator can grant the code and cannot typo it.

The store deliberately accepts codes it has never heard of (grammar-checked only): the vocabulary
check lives at the operator boundary, where the merged set is known. Mind the default: a code
absent from a tariff's list is *denied* for that tariff's users — after adding a feature, existing
plans grant it only once the operator adds the code to them. Users without a tariff have
everything, as always.

### The service

`LimitService` (`tariffs/service.py`) implements the `LimitGate` port consumers depend on. The
tariff lookup is one SELECT by unique key per user action and is deliberately uncached — an
operator's change applies at once; the day's totals (the sum-over-window query) are cached for 5 s
and invalidated by the node's own `record`. Enforcement is check-then-consume:
the run admitted last before exhaustion completes in full, and two nodes can each admit one run at
the boundary — limits are guardrails, not accounting. A tariff change applies from the user's next
run. `check_run_budget` is the port's only budget question; every run-start path asks it and no
other code path checks budgets at all.

### Operator surface

The console's «Тарифы» tab manages the catalog and the bindings (`GET/POST/DELETE /api/admin/tariffs`,
`POST /api/admin/tariffs/assign`; audited as `tariff.*`); «Потребление» shows the aggregated report
(`GET /api/admin/usage?days=…`) with a drill-down into the raw ledger (`GET /api/admin/usage/events`).
The ledger joins the retention sweep through `OF_RETENTION_USAGE_DAYS`
(see [configuration.md](configuration.md)); 0 keeps every event forever.

## Invariants

- **Zero configuration = zero behavior change.** No tariffs, no bindings — every check passes.
- **The ledger is append-only.** Reports and limit checks are sums; nothing updates an event.
- **A message is never refused, only its run is withheld.** It is persisted, counted and waits in
  its OPEN exchange for the budget to reset — nothing the user sends is lost.
- **The N-th message under a limit of N is still answered**: the message is counted at intake, so
  the run-start comparison for `daily_user_messages` is strict.
- **Feature gating is defence in depth**: hidden from the spec list *and* refused inside `execute`.
- **Both entrances enforce a cap with the same words** — the agent tool and the HTTP endpoint share
  `job_quota_refusal`.

## Failure modes

| Situation | Outcome |
|---|---|
| Metering write fails | The event is lost and logged; the run is unaffected |
| Two nodes admit at the budget boundary | Both runs complete; the overshoot is bounded by in-flight work |
| Tariff changed mid-run | The old feature set finishes the run; the next run sees the new one |
| Cron budget exhausted all day | One notice per job per day; the job fires again after midnight UTC |
| Deleting a tariff with users bound | Refused (HTTP 409) until the users are reassigned |

## Code anchors

- `core/src/octoforge_core/tariffs/api.py` — `FeatureCode`, `UsageEvent`, the `LimitGate` port
- `core/src/octoforge_core/tariffs/service.py` — `LimitService`: checks, caches, metering
- `core/src/octoforge_core/tariffs/store.py` — the catalog store and the insert-only meter
- `core/src/octoforge_core/agent/runner.py` — submit/wake/spawn checks and run metering
- `core/tests/test_limit_service.py`, `core/tests/test_tariffs_store.py`,
  `core/tests/test_conversation_runner.py`
