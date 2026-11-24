# 0003. Every tool lives behind an MCP server, one server per data domain

**Context.** Agents need documents, SQL, live APIs and write actions. Calling these as in-process Python
functions would put every credential in the orchestrator's process, next to untrusted text.

**Decision.** Four MCP servers over streamable HTTP: `knowledge`, `warehouse`, `live-data` and `actions`.
Each is an OAuth resource server with its own canonical resource id, checks audience and scope on every
request (an ASGI guard before the MCP layer, plus a check inside each tool), and holds its own credentials.
The orchestrator uses `langchain-mcp-adapters` to turn MCP tools into LangChain tools.

**Why.**
- **Credentials stay with the data.** The warehouse password is only in the warehouse server. The
  orchestrator can't run arbitrary SQL even if it wanted to; it can only call `run_sql`, which is guarded.
- **One permission model.** Scopes per tool are declared in `config/scopes.yaml`, carried in each tool's
  `_meta`, and cross-checked when the catalog loads. A mismatch disables the tool.
- **A standard boundary.** Any MCP client (Claude Desktop, an IDE) could use the same servers with the same
  tokens.

**Notes.** The servers run the official MCP Python SDK in stateless HTTP mode with JSON responses (protocol
revision 2025-11-25 at the time of writing). Stateless mode means any replica can answer any request and
there's no session to hijack; each call carries its token.

**Trade-off.** A network hop per tool call (a few milliseconds locally) and more services to run. The
`mara dev` runner and Docker Compose hide most of that.
