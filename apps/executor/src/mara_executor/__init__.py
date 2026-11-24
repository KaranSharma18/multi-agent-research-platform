"""The action executor.

Plain code, no model calls. It wakes on Postgres NOTIFY (and polls as a
backstop), claims one approved action at a time with `FOR UPDATE SKIP LOCKED`
so several executors can run side by side, re-checks the approval token
locally, gets a client-credentials token with only the scope that tool needs,
and calls the actions MCP server with the approval token and an idempotency key
in the request's `_meta`.

Why a separate process: the orchestrator reads untrusted text all day and must
never hold write credentials. The executor holds them but never reads
untrusted text; it only carries out exactly what a person approved.
"""
