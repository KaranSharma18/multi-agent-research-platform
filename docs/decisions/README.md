# Design decisions

Short records of the decisions that shape MARA. Each one says what the problem was, what we chose, why,
and what it costs. They're written to be argued with: if a reason no longer holds, change the decision and
write a new record.

| # | decision |
|---|---|
| [0001](0001-uv-workspace-monorepo.md) | One repo, one uv workspace, one package per service |
| [0002](0002-plan-then-act-supervisor.md) | A supervisor graph that plans first, then fans out workers |
| [0003](0003-mcp-for-every-tool.md) | Every tool lives behind an MCP server, one server per data domain |
| [0004](0004-per-task-tokens.md) | One short-lived, scoped token per task, exchanged from the user's token |
| [0005](0005-dynamic-tool-loading-pinned-catalog.md) | Workers load tools on demand from a hash-pinned catalog |
| [0006](0006-evidence-and-spotlighting.md) | Treat every tool result as untrusted evidence, with ids and citations |
| [0007](0007-approval-tokens-and-executor.md) | Writes need a typed proposal, a policy check, a person, and a separate executor |
| [0008](0008-a2a-research-agent.md) | Web research is a remote agent behind A2A |
| [0009](0009-gemini-two-tiers-offline-model.md) | Gemini in two tiers, plus an offline stand-in |
| [0010](0010-hybrid-retrieval-qdrant.md) | Hybrid retrieval in Qdrant with ACL filters |
| [0011](0011-read-only-warehouse.md) | Warehouse access through a guard, views, grants and row-level security |
| [0012](0012-durable-runs.md) | Postgres for checkpoints, events and recordings; SSE to clients |
| [0013](0013-evals-from-seeded-world.md) | Evals generated from the same seeded world as the data |
| [0014](0014-dev-token-service.md) | A small dev token service, with Keycloak as the drop-in alternative |
| [0015](0015-windows-first-dev-experience.md) | Windows-first developer experience |
