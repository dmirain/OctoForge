"""Collections: structured HTTP responses the agent queries instead of reading.

A large JSON or CSV response used to be cut at a character limit and the tail
thrown away. Here it becomes a *collection* — rows in two fixed tables, one
row per element — and the model receives a passport (shape, counts, a ref)
instead of a truncated head, then queries the data where it lies.

The query engine compiles a small typed DSL into SQL over Postgres jsonb; on
any other dialect the feature simply is not wired and responses keep the old
truncation behavior.
"""
