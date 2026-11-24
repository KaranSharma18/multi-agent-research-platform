# 0011. Warehouse access through a guard, views, grants and row-level security

**Context.** Letting a model write SQL is useful and risky: wrong joins double count, and a bad statement
could write or read what it shouldn't.

**Decision.** Four independent layers:

1. **SQL guard** in the MCP server: one statement, `SELECT`/`WITH` only, only `analytics` views, a deny
   list of functions (`pg_*`, `set_config`, `lo_*`, ...).
2. **Views and grants:** `agent_ro` can read only the `analytics` views; personal-data columns in `core` are
   not granted at all, so even a bypassed guard can't read them.
3. **Row-level security:** policies on `core` tables filter by `app.regions`, which the server sets per
   query from the token. Views are `security_invoker`, so policies apply to the caller.
4. **Role defaults:** `default_transaction_read_only` and a 5 second `statement_timeout`; each query also
   runs in an explicit read-only transaction.

Results page through a signed `result_id`, so a model can read more rows without re-running or editing SQL.
View and column comments describe the data, and `describe_table` returns them, which is what lets the model
write correct joins (for example "count distinct customer_id").

**Trade-off.** Some questions need a view that doesn't exist yet; that's a schema change with review, which
is the right speed for exposing data to an agent.
