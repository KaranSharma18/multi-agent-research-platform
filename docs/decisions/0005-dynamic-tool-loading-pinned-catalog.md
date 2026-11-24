# 0005. Workers load tools on demand from a hash-pinned catalog

**Context.** Showing a model every tool it might need costs tokens and accuracy: with 15 tool schemas in
context, small models pick wrong tools more often. And MCP tool descriptions come from the server, so a
changed description is a way to steer the model ("tool poisoning").

**Decision.**
- Each worker starts with 1 to 3 **core tools** for its kind plus `find_tools`. `find_tools("what I need")`
  ranks the catalog with BM25, *only within the task's scopes*, and the next model call sees up to three
  more tools. Implemented as LangChain middleware that filters `request.tools` per call.
- The orchestrator loads each server's catalog with a catalog-only token and hashes every tool (name,
  description, input schema, scope). Hashes are pinned in `mcp-tools.lock.json`. A tool whose hash changes
  is disabled until someone reviews it and runs `uv run mara tools lock`.

**Why.** Small prompts for the common case, room to grow the catalog, and a reviewable diff whenever a tool
changes, much like a dependency lock file.

**Trade-off.** One extra step when a worker needs a non-core tool, and a lock file to maintain. In `prod`
mode a missing lock file stops startup; in dev it logs a warning.
