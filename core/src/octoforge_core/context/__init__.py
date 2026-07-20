"""Self-contained context module: dialog history compaction and archive search.

The module boundary is `octoforge_core.context.api`: the `ContextCompactor`
port (assembles a process branch as topics block + hot tail), the
`SummaryStore` and `MessageArchive` ports and the DTOs. Everything else (SQL
storage, the LLM-driven compactor) is an implementation detail; the store can
be extracted behind an HTTP boundary later without changing call sites.
"""
