# OctoForge documentation

This describes OctoForge as it is: a self-hosted, multi-user agent platform whose knowledge, skills
and integration contracts live in a database, with a typed Python core (`octoforge-core`) that other
applications embed. The repository's [README](../README.md) is the pitch; this is the manual.

Every page describes current behavior and is checked against the code. If a page and the code
disagree, the page is wrong — see [CONVENTIONS.md](CONVENTIONS.md) and open an issue.

## Start here

| If you want to | Read |
|---|---|
| understand what this is and why it is shaped this way | [concept.md](concept.md) |
| know what the words mean (exchange, narrative, material…) | [glossary.md](glossary.md) |
| see how the pieces fit and what is swappable | [architecture.md](architecture.md) |
| run it | [guides/quickstart.md](guides/quickstart.md) |
| deploy it | [guides/deployment.md](guides/deployment.md) |
| embed the core in your own application | [guides/embed-the-core.md](guides/embed-the-core.md) |
| know what it does not do | [limitations.md](limitations.md) |
| review its security posture | [security.md](security.md) |

## Reference

One file per aspect. Each has the same shape: purpose, how it works, invariants, configuration,
failure modes, code anchors.

**Conversation runtime**

- [reference/agent-loop.md](reference/agent-loop.md) — the LLM ↔ tools event loop: streaming, eager
  tool execution, cancellation, retries, iteration cap
- [reference/conversation-actor.md](reference/conversation-actor.md) — the per-dialog actor:
  narrative, processes, branches, event broadcast, delivery, restart recovery
- [reference/exchanges.md](reference/exchanges.md) — obligations to the user: lifecycle, material
  collections, `ask_user`, nudges
- [reference/routing.md](reference/routing.md) — deciding which exchange an incoming message belongs
  to
- [reference/context-compaction.md](reference/context-compaction.md) — rolling summaries, the hot
  tail, overflow handling, history search
- [reference/tasks.md](reference/tasks.md) — background work as task rows and processes
- [reference/cron.md](reference/cron.md) — the scheduler, leases, missed firings

**Knowledge, data and tools**

- [reference/tools-framework.md](reference/tools-framework.md) — what a tool is, how visibility and
  errors work
- [reference/instructions.md](reference/instructions.md) — skills, knowledge, endpoint records:
  ranking, ownership, the system registry and its overlay
- [reference/datasets.md](reference/datasets.md) — user datasets with JSON-schema validation
- [reference/memory.md](reference/memory.md) — per-user memories
- [reference/endpoints-and-net.md](reference/endpoints-and-net.md) — outbound HTTP: `http_request`,
  `endpoint_get`, `external_call`, the SSRF guard
- [reference/secrets.md](reference/secrets.md) — the encrypted per-user secret store
- [reference/search.md](reference/search.md) — web search
- [reference/vision-and-speech.md](reference/vision-and-speech.md) — images and voice messages
- [reference/llm-clients.md](reference/llm-clients.md) — LLM, embedding and reranker clients
- [reference/data-model.md](reference/data-model.md) — tables, dialects, time, migrations

**Surfaces**

- [reference/http-api.md](reference/http-api.md) — REST + SSE, authentication, schemas
- [reference/telegram.md](reference/telegram.md) — the bot: polling, rendering, invites, admin tool
- [reference/admin-console.md](reference/admin-console.md) — the operator console and its read model
- [reference/configuration.md](reference/configuration.md) — every `OF_*` variable and what it turns
  on

## Guides

- [guides/quickstart.md](guides/quickstart.md) — from a clone to a running agent
- [guides/deployment.md](guides/deployment.md) — production topology and day-2 operations
- [guides/embed-the-core.md](guides/embed-the-core.md) — using `octoforge-core` as a library
- [guides/add-a-tool.md](guides/add-a-tool.md) — give the agent a new capability in code
- [guides/add-a-surface.md](guides/add-a-surface.md) — put the agent on another channel
- [guides/author-skills-and-endpoints.md](guides/author-skills-and-endpoints.md) — teach it without
  writing code
- [guides/performance.md](guides/performance.md) — where latency comes from and how to keep it low

## Comparisons

Code-level studies of adjacent projects, and what OctoForge takes or refuses from each:
[comparisons/README.md](comparisons/README.md).

## Also in the repository

- [../AGENTS.md](../AGENTS.md) — conventions for writing code here (aimed at AI coding agents)
- [../CONTRIBUTING.md](../CONTRIBUTING.md) — how to contribute
- [archive/](archive/) — the Russian working notes this project grew from. Unmaintained, kept for
  research value only.
