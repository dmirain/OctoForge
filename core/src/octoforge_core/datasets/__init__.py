"""Self-contained datasets module: user-owned structured data stores (trackers).

The module boundary is `octoforge_core.datasets.api`; everything else (SQL
storage, embeddings, ranking, record validation) is an implementation detail
of the local implementation. The module only stores, validates and searches —
it mirrors the instructions module and can likewise be extracted behind an
HTTP boundary later.
"""
