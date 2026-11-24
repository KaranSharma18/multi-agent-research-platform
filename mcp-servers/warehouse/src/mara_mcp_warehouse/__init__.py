"""Warehouse MCP server: SQL the agent can run, and nothing else.

Four independent layers keep `run_sql` read-only and in bounds:
  1. a sqlglot parse that accepts exactly one SELECT over the `analytics` schema
     and refuses dangerous functions,
  2. the `agent_ro` database role, which can only SELECT from analytics views,
  3. a read-only transaction with a statement timeout,
  4. row-level security keyed on the caller's region groups from the token.
Any one of them alone would stop most mistakes; together they stop a fooled model.
"""
