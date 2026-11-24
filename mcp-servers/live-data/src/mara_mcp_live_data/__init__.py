"""Live-data MCP server: nine read tools over the incident API, the status page,
GitHub and an FX rates API.

It is the server with the most tools, which is why workers load its tools on
demand instead of seeing all nine schemas in every prompt. It calls upstream
APIs with its own read-only credentials and never forwards the caller's token.
"""
