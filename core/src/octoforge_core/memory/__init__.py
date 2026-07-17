"""Self-contained memory module: durable per-user and global agent memories.

The module boundary is `octoforge_core.memory.api`; everything else (SQL
storage) is an implementation detail of the local store. The module only
stores, finds and deletes memories keyed by (owner, key) — it mirrors the
instructions/datasets modules and can likewise be extracted behind an HTTP
boundary later.
"""
