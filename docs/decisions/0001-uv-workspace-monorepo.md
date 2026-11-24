# 0001. One repo, one uv workspace, one package per service

**Context.** MARA has ten Python services and libraries that share code (auth, PII masking, settings) and
must agree on contracts (action argument shapes, scope names). Separate repos would make every contract
change a multi-repo release.

**Decision.** A single repository managed as a [uv workspace](https://docs.astral.sh/uv/concepts/projects/workspaces/).
Each service is its own package (`apps/*`, `mcp-servers/*`, `agents/*`) with its own dependencies; shared
code lives in `packages/mara-core`. One `uv.lock` pins every dependency for every package.

**Why.**
- `uv sync` gives a working environment in seconds, on Windows too, with Python itself managed by uv.
- Each Docker image installs only its package's dependencies (`uv sync --package X --frozen`), so the
  actions server doesn't ship LangChain and the orchestrator doesn't ship the Claude Agent SDK.
- A contract change (say, a new field on `CreateGithubIssue`) is one commit that updates the orchestrator,
  executor and actions server together, and the tests see all three.

**Trade-off.** All packages share one lock, so two services can't pin different versions of the same
library. For a platform this size that's a feature: one version of `mcp` everywhere means client and server
always speak the same protocol revision.
