# Limitations

What OctoForge does not do. Two kinds: **decisions** (absent because absence is the design) and **gaps** (absent
because nobody has written them). Both matter when you are deciding whether this fits; silence would read as
"supported".

## Decisions

**No shell and no filesystem tools.** The agent acts through declared, schema-validated HTTP contracts only.
This removes the approval / sandbox / policy machinery an exec-capable agent needs, and the class of incident
that comes with it. The cost: it cannot fix a server, run a script or edit a repository. It is not a coding
agent.

**No runtime code loading.** Capability arrives as data (records), never as importable plugins. Adding a *code*
tool means a deploy.

**No agent-to-agent orchestration.** No graphs, crews or role-playing sub-agents. Concurrency is one process
per obligation; capability is one stored record. Anything more has to justify itself against those two.

**No MCP.** Tools are ports and records, not an external protocol. Interoperability with the MCP ecosystem is
not available.

**One shared operator credential for the HTTP surface** rather than a user system. The agent's users are
identified per surface (Telegram's identity, or your proxy's header), not by accounts in this application.

**Dialog isolation by `(user_id, channel)`.** The same person's web and Telegram conversations are separate
dialogs with separate narratives; there is no cross-channel identity linking. Memories and stored records
*do* follow the user across surfaces.

**Cron expressions only.** No `every 15m` / `at 18:00 tomorrow` syntax; one-time jobs are a dated expression
plus `one_shot`. The agent composes the expression, which is why users can still speak naturally.

**No automatic retry of failed scheduled work.** A failure is recorded (`last_status`, `last_error`) and the
next regular firing happens on time. One shot means one attempt.

**Streaming deltas are not durable.** A subscriber that falls behind loses tokens; terminals are always
delivered. Scrollback comes from stored messages, not from replaying the stream.

## Gaps

### Security

- **DNS rebinding (TOCTOU) in the SSRF guard.** The address is validated at check time and resolved again by
  the HTTP client at connect time. Closing it needs connecting by resolved IP with an explicit `Host` header.
- **No per-user authentication on the HTTP surface.** `X-User-Id` is a trusted string; the deployment expects
  an authenticating proxy. Without one, treat the HTTP surface as operator-only.
- **No per-tool authorization policy.** A user's runs can use every registered tool; there is no way to say
  "this group may not call `http_request`".
- **No content-level prompt-injection defense.** Injected instructions cannot reach secrets or a shell, but
  they can still make the agent call a permitted tool with attacker-chosen arguments. `OF_HTTP_REQUEST_ALLOWLIST`
  closes the exfiltration half when an installation knows which origins it needs.
- **The audit trail is a log, not a queryable record.** Operator actions are logged
  (`audit action=… actor=… target=…`), but there is no retention policy, no UI and no tamper evidence.

### Operations

- **No rate limiting and no quotas** on *usage* — per user or per installation. Nothing stops one dialog from
  spending an unbounded amount of provider tokens; the only bounds are the per-dialog process limit and the
  per-run iteration cap. (Failed operator logins *are* rate limited — see [security.md](security.md).)
- **Token usage is recorded but not aggregated.** Per-assistant-message counts are stored; there is no cost
  reporting, per-user total or budget alert.
- **No metrics endpoint.** No Prometheus surface, no traces; observability is stdout logs plus the operator
  console.
- **Single writer for everything except cron.** The cron scheduler is safe on several instances (SQL lease),
  but nothing else has been designed for horizontal scale, and SQLite allows exactly one writer.
- **A failed Alembic upgrade falls back to `create_all`** at startup, which is right for a fresh database and
  wrong for an existing one — an existing database with a failed migration needs manual attention.
- **No provider failover.** One LLM endpoint is configured; if it is down, runs fail (after retries). Routing
  between several providers or models is a composition-root change you write yourself.
- **The router uses the main model.** A cheaper model for routing is not configurable without replacing the
  port.

### Agent behavior

- **No loop detection beyond the iteration cap.** A model repeating the same tool call is only stopped by
  `OF_AGENT_MAX_ITERATIONS`.
- **No structured-output contract.** Answers are text; there is no schema-constrained final result.
- **Retrieval quality is the ceiling.** If `recall` does not surface a record, the capability effectively does
  not exist for that request. Ranking is cosine plus an exact-title boost plus an optional rerank — there is no
  hybrid keyword search, no MMR diversification and no decay.
- **No lifecycle for learned records.** Usage counters exist, but nothing promotes, retires or reviews stored
  skills; curation is manual.
- **Endpoint parameters are strings only.** Complex request bodies cannot be expressed in an endpoint record —
  that is where a code tool starts.
- **Dataset schemas are immutable.** Changing shape means a new dataset.
- **Tool responses are truncated by character count** (8000 for `external_call`, 4000 for `http_request`), not
  by tokens and not by extracting the relevant part. A large HTML page wastes context.

### Surfaces

- **Telegram: private chats only.** No groups, no forum topics, no channels. No status reactions, and the
  typing indicator is sent once per answer rather than kept alive across a long run.
- **The Telegram update offset lives in memory**, so updates that arrive during a restart window can be missed.
- **The web chat UI is a demo surface.** It streams the current session's events; it does not load past
  history, and it has no notion of accounts.
- **The operator console is Russian-only** and has no localization mechanism.
- **No file attachments beyond images and voice.** Documents, video and audio files other than voice notes are
  not ingested.

## What this list is for

If something here blocks you, it is a design conversation rather than a bug report — several of these are
one-module changes behind an existing port (provider failover, pgvector ranking, an external scheduler), and a
few are deliberate walls (no shell). The reference pages name the port each one lives behind.
