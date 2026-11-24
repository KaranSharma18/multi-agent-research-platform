"""Sandbox versions of the outside world.

The live-data and actions MCP servers talk to these endpoints in `sandbox` /
`mock` mode exactly as they would talk to the real APIs, over HTTP. That keeps
demos and evals deterministic (a nightly score never moves because GitHub
changed) and means a mistake can never reach a real person.
"""
