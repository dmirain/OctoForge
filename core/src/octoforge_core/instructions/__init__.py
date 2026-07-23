"""Self-contained instructions module: store, search and rank knowledge/skills/endpoints.

The module boundary is `octoforge_core.instructions.api`; everything else
(SQL storage, embeddings, ranking) is an implementation detail of the local
implementation. Execution of endpoint records lives outside the module, in
core (`octoforge_core.net`).
"""
