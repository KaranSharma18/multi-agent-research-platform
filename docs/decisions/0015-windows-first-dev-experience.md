# 0015. Windows-first developer experience

**Context.** The main developer works on Windows. Python async libraries have Windows quirks, and Docker is
not always available.

**Decision.**
- **uv** for Python, dependencies and the workspace; `uv sync` and `uv run` work the same in PowerShell.
- **Event loops:** psycopg's async driver can't use Windows' default Proactor loop, so services that touch
  Postgres run on a `SelectorEventLoop` (`mara_core.serve`). The research agent is the exception (the Claude
  Agent SDK starts a subprocess, which needs Proactor) and it doesn't use Postgres.
- **`mara dev`** starts every service as a child process with prefixed logs and health-checked stages, and
  stops them cleanly on Ctrl+C (`CTRL_BREAK_EVENT` on Windows). No Docker needed for the services.
- **Qdrant embedded mode** (a folder) removes the vector database server for local work.
- Paths are resolved from the repo root, `.gitattributes` fixes line endings, and every command in the docs is
  written for PowerShell first.

**Trade-off.** A little platform-specific code in one module (`serve.py`, `devrun.py`), in exchange for
"clone, `uv sync`, run" on any machine.
