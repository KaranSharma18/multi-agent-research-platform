"""The orchestrator: a LangGraph supervisor and the API around it.

Package map:
  graph/     the state machine: plan -> dispatch -> workers -> collect ->
             synthesize -> verify -> (propose -> approve -> execute) -> finalize
  tools/     the tool registry, dynamic tool loading, MCP interceptors
  auth/      the token broker that mints one scoped token per task
  a2a/       the client for the remote research agent
  llm/       Gemini models with fallbacks, the offline model, cost tracking
  api/       FastAPI routes: runs, events (SSE), approvals, replay
  db/        the app database schema and queries
"""
