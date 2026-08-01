# Web search

One optional tool for public facts the installation does not already know. It is deliberately positioned
*below* the stored knowledge: what the organization knows lives in the instruction store, and search is
what you use when nobody stored anything.

## How it works

`SearchProvider` is a port with one method — `search(query, num_results) -> SearchResponse` — returning
transport-neutral DTOs: organic hits (`title`, `link`, `snippet`) plus an optional direct answer. Neither
the tool nor a replacement provider deals with a vendor payload.

The shipped implementation is `SerperSearchProvider` (serper.dev, Google results). It is registered only
when `OF_SERPER_TOKEN` is set; without it the `web_search` tool does not exist, so the model is never
offered a capability that would fail.

Replacing the provider (Bing, Brave, Tavily, an internal search service) means implementing the port and
registering `WebSearchTool` with it in your composition root — no core change.

The tool's description does the positioning work: it presents itself as being for public facts only and
points at `recall` for anything the installation should already know.

## Invariants

- **The tool exists only when a provider is configured.**
- **Provider payloads never leak upward** — the port's DTOs are the contract.
- **Backend failures are surfaced as `SearchError`** and become normal tool-error output for the model,
  not a failed run.
- **The number of results is bounded by the caller**, and the provider maps at most that many hits.

## Configuration

| Variable | Effect |
|---|---|
| `OF_SERPER_TOKEN` | serper.dev API key; empty hides the `web_search` tool entirely |

## Failure modes

| Situation | Outcome |
|---|---|
| No token configured | Tool absent; the agent uses stored knowledge or `http_request` |
| Network error, bad status, quota exhausted | `SearchError` → `error: …` output; the run continues |
| Provider returns an unexpected payload shape | Parsed defensively; missing fields are skipped |

## Code anchors

- `core/src/octoforge_core/search/api.py` — `SearchProvider`, `SearchResponse`, `SearchResult`, `SearchError`
- `core/src/octoforge_core/search/serper.py` — the serper.dev implementation
- `core/src/octoforge_core/search/tools.py` — the `web_search` tool
- `deploy/src/octoforge_deploy/main.py` — conditional registration
- `core/tests/test_serper_provider.py`
