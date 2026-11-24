# 0012. Postgres for checkpoints, events and recordings; SSE to clients

**Context.** Runs take seconds to minutes and may pause for approval for days. Clients disconnect. Processes
restart.

**Decision.** Postgres holds:

- LangGraph checkpoints (`AsyncPostgresSaver`) after every step;
- `run_events`, the stream clients follow; a trigger sends `NOTIFY` so every orchestrator replica can push
  new events to its SSE clients;
- `tool_calls`, masked recordings of every MCP call (the basis for replay);
- `actions`, with a trigger that wakes the executor;
- `audit_log`, hash-chained.

Clients use Server-Sent Events with `Last-Event-ID`. The user's token is deliberately not persisted.

**Why.** One store that the team already knows, transactional with the rest of the state, and enough for
this scale. SSE is one-directional, which is all a run feed needs, and it passes through proxies easily.

**Trade-off.** A run that crashes mid-research can't resume (it would need new task tokens) and is marked
failed with a clear message; runs past approval resume automatically. At higher scale, events and recordings
could move to a log store; the API wouldn't change.
