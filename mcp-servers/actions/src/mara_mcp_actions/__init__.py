"""Actions MCP server: GitHub issues, Slack messages and (disabled) email.

Before anything is written, a call must pass five checks, in this order:
  1. a valid token for this server issued to the executor's client identity,
  2. the tool's write scope in that token,
  3. an approval token signed by the orchestrator, unexpired, naming this tool
     and the SHA-256 of these exact arguments,
  4. an approval `jti` this server has never seen (one approval, one write),
  5. allowlisted targets (repo, channel, email domain), checked again here.

Writes carry an idempotency key, so a retried call returns the first result
instead of opening a second issue.
"""
