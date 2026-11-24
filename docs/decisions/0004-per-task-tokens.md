# 0004. One short-lived, scoped token per task, exchanged from the user's token

**Context.** A worker answering "what's our ARR in EMEA?" needs warehouse read access *as that user*, so
row-level security and document ACLs apply. Giving the orchestrator a service account with broad access
would make every user an admin by proxy (the confused deputy problem).

**Decision.** For each task, the orchestrator performs an OAuth 2.0 Token Exchange (RFC 8693): it presents
the user's token and asks for a new token with:

- `aud` = the one MCP server that task kind uses,
- `scope` = the scopes `config/scopes.yaml` lists for that kind (`sql:read`, ...),
- the user's `sub` and groups, plus `act` naming the orchestrator.

The token service enforces a policy (`config/auth/dev-realm.yaml`) on which audiences and scopes the
orchestrator may request. Tokens last five minutes and are never logged or stored. For catalog listing and
the A2A call, the orchestrator uses client-credentials tokens as itself (`catalog:read`, `research:run`),
with no user identity.

**Why.**
- **Least privilege by construction.** A docs worker's token can't call the warehouse (wrong audience) and
  can't read GitHub (wrong scope), even if its model tries.
- **The server enforces the user's rights.** The warehouse reads regions from the token; the knowledge server
  reads groups. The orchestrator can't widen them.
- **Blast radius is one task, five minutes.**

**Trade-off.** One token request per task (cached until near expiry). The models never see or pick scopes;
scope selection is a table in config, so a new task kind is a config change plus a review.
