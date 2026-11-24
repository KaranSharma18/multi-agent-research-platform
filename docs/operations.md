# Operations

## Ports

| service | port | health |
|---|---|---|
| orchestrator (API, SSE) | 8000 | `/healthz`, `/readyz`, `/metrics`, `/docs` |
| token service | 8090 | `/healthz`, `/jwks.json` |
| mcp-knowledge | 8101 | `/healthz`, `/metrics` |
| mcp-warehouse | 8102 | `/healthz`, `/metrics` |
| mcp-live-data | 8103 | `/healthz`, `/metrics` |
| mcp-actions | 8104 | `/healthz`, `/metrics` |
| web research agent | 8200 | `/healthz`, `/.well-known/agent-card.json` |
| mock APIs | 8300 | `/healthz` |
| UI (dev / container) | 5173 / 3000 | |
| Postgres / Qdrant | 5432 / 6333 | |

## Running on Windows

Everything works from PowerShell with uv.

- **Postgres without Docker:** install PostgreSQL 16 from postgresql.org, then
  `uv run mara db init --superuser-url postgresql://postgres:<your password>@localhost:5432/postgres`.
- **Qdrant without Docker:** set `QDRANT_URL=data/generated/qdrant` in `.env`. Only one process can open the
  folder, so run `uv run mara ingest` and `uv run mara eval retrieval` while the knowledge server is stopped.
- **Event loops:** handled for you (`mara_core.serve`). If you write a new script that uses psycopg's async
  API on Windows, run it with `mara_core.serve.run_coroutine`.
- **Ctrl+C** in `uv run mara dev` stops every child process. If a port is still busy afterwards, find the
  process with `netstat -ano | findstr :8000` and end it in Task Manager.
- Long paths: if `uv sync` fails on very long paths, enable long paths
  (`git config --global core.longpaths true`) and clone to a short folder like `C:\src\mara`.

## Configuration

All settings come from environment variables or `.env` (see `.env.example`, every option is commented).
Policy lives in versioned files under `config/`:

| file | what it controls |
|---|---|
| `scopes.yaml` | task kinds → audience and scopes; tool → scope; core tools per kind |
| `allowlists.yaml` | repos, channels, email domains and link domains actions may touch; limits |
| `prices.yaml` | per-model token prices used for budgets and cost reports |
| `research_domains.yaml` | domains the research agent may and may not fetch |
| `prompts/*.md` | versioned prompts (`<!-- version: name@N -->`), stamped on every run |
| `auth/dev-realm.yaml` | dev users, clients and token-exchange policy |

Restart the services after changing them.

## Tracing

- **LangSmith:** set `LANGSMITH_TRACING=true`, `LANGSMITH_API_KEY` and `LANGSMITH_PROJECT`. Every graph
  node, worker loop and model call shows up with run id and prompt versions in the metadata.
- **OpenTelemetry:** `docker compose --profile observability up` starts Jaeger; set
  `OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318` (or `http://jaeger:4318` inside Compose). HTTP calls
  between services are traced.

## Switching to Keycloak

`infra/keycloak/realm-mara.json` mirrors the dev realm: the same users (password = user name), groups,
realm roles, clients and scopes. It has been tested with Keycloak 26.8.2 for every flow MARA uses: password
login for `mara-ui`, standard token exchange (V2) by `mara-orchestrator`, and client credentials for the
orchestrator's catalog and research tokens and for the executor.

```powershell
docker compose --profile keycloak up -d keycloak
```

**Services on your machine (`mara dev`)**: set in `.env` and restart:

```
AUTH_ISSUER=http://localhost:8080/realms/mara
AUTH_TOKEN_URL=http://localhost:8080/realms/mara/protocol/openid-connect/token
AUTH_JWKS_URL=http://localhost:8080/realms/mara/protocol/openid-connect/certs
```

**Services in Compose**: keep the same `AUTH_ISSUER`, but reach Keycloak by its service name:
`AUTH_TOKEN_URL=http://keycloak:8080/realms/mara/protocol/openid-connect/token` and
`AUTH_JWKS_URL=http://keycloak:8080/realms/mara/protocol/openid-connect/certs`. The Keycloak service sets
`KC_HOSTNAME=http://localhost:8080` so every token carries the same issuer whichever address issued it. Build
the UI with `VITE_TOKEN_URL=http://localhost:8080/realms/mara/protocol/openid-connect/token`.

How the realm maps MARA's model onto Keycloak:

- Each MCP server and the research agent is a client named by its canonical resource id, so it can be a
  token audience.
- Each scope (`sql:read`, `docs:read`, ...) is a client scope with an audience mapper for the server it
  belongs to. The orchestrator may request only the read scopes, so exchanging for `github:issues:write` fails
  with `invalid_scope`; only the executor has the write scopes.
- A `groups` scope emits flat group names (`eng`, `region:na`), and roles come from `realm_access.roles`,
  which MARA reads alongside a top-level `roles` claim.
- User ids (`sub`) are UUIDs in Keycloak; MARA shows `preferred_username` everywhere a person reads it.

## Live actions

By default actions go to the mock APIs (`data/generated/sandbox/` shows what was "sent"). To write for
real, set `ACTIONS_BACKEND=live` with `GITHUB_TOKEN` (fine-grained, issues: write on the allowlisted repos
only) and `SLACK_WEBHOOK_URL`, and edit `config/allowlists.yaml` to your repos and channels. Email stays off
unless `email_enabled: true`.

## Common problems

| symptom | cause and fix |
|---|---|
| `tool_lock_missing` warning | run `uv run mara tools lock` while the servers are up, then restart the orchestrator |
| `tool_pin_mismatch` error | a tool's description or schema changed; review the change, then `mara tools lock` |
| worker says "no tools are available" | an MCP server is down or all its tools are disabled; check `uv run mara dev` logs and `/readyz` |
| `Gemini embeddings need GOOGLE_API_KEY` | set the key, or `EMBEDDING_PROVIDER=hash` and `LLM_PROVIDER=offline` |
| changed `EMBEDDING_PROVIDER` and search is empty | vectors from different embedders don't mix; re-run `uv run mara ingest` |
| `role agent_ro does not exist` during seed | run `uv run mara db init` (or let Docker's Postgres run `infra/postgres/init.sql`) |
| run failed "orchestrator restarted" | runs in progress during a restart can't continue without the user's token; ask again |
| approval never executes | is the executor running? check its logs; approvals expire after 10 minutes if it was down |
| Qdrant "already accessed by another instance" | embedded mode allows one process; stop the knowledge server first |

## Resetting

```powershell
uv run mara seed --as-of 2026-11-21   # rebuild the world for a fixed date (evals use the seeded date)
uv run mara ingest
```

To wipe runs and actions, drop and recreate the `mara` database (or `docker compose down -v` to remove all
volumes).
