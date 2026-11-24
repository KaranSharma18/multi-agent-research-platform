"""Tools: search_docs, fetch_chunk, list_sources."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from mara_core.mcpserver import (
    EmptyResult,
    apply_fault,
    asgi_app,
    build_server,
    require_scope,
    tool_metrics,
)
from mara_core.retrieval.embeddings import make_embedder
from mara_core.settings import Settings, get_settings
from mcp.server.fastmcp import Context
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from mara_mcp_knowledge.store import KnowledgeStore

SERVER = "knowledge"
INSTRUCTIONS = (
    "Search Acme Cloud's postmortems, runbooks and product docs. search_docs returns cited chunks; "
    "fetch_chunk returns one chunk in full."
)


def create_server(settings: Settings | None = None, store: KnowledgeStore | None = None):
    settings = settings or get_settings()
    mcp, verifier = build_server(
        name="mara-knowledge", resource=settings.knowledge_resource, instructions=INSTRUCTIONS, settings=settings
    )
    store = store or KnowledgeStore(settings, make_embedder(settings))
    read_only = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)

    @mcp.tool(annotations=read_only, meta={"com.mara/scope": "docs:read"})
    async def search_docs(
        ctx: Context,
        query: Annotated[
            str, Field(description="What to look for. Name services, incident ids or error codes when you know them.")
        ],
        top_k: Annotated[int, Field(ge=1, le=10)] = 6,
        sources: Annotated[
            list[
                Literal[
                    "postmortem",
                    "runbook",
                    "policy",
                    "product",
                    "account",
                    "finance",
                    "rfc",
                    "faq",
                    "release",
                    "status",
                ]
            ]
            | None,
            Field(description="Limit to these document types"),
        ] = None,
    ) -> dict[str, Any]:
        """Hybrid search over postmortems, runbooks and product docs. Returns up to top_k chunks with
        title, source, date and a relevance score. Newer documents supersede older ones on the same topic."""
        principal = require_scope(SERVER, "search_docs")
        with tool_metrics(SERVER, "search_docs"):
            try:
                await apply_fault(ctx, settings)
            except EmptyResult:
                return {"hits": []}
            hits = await store.search(query, groups=principal.acl_groups(), top_k=top_k, sources=sources)
            return {"hits": hits}

    @mcp.tool(annotations=read_only, meta={"com.mara/scope": "docs:read"})
    async def fetch_chunk(
        ctx: Context,
        chunk_id: Annotated[str, Field(description="A chunk_id returned by search_docs")],
    ) -> dict[str, Any]:
        """Return one chunk in full, with its document's title, source and date."""
        principal = require_scope(SERVER, "fetch_chunk")
        with tool_metrics(SERVER, "fetch_chunk"):
            await apply_fault(ctx, settings)
            hit = await store.fetch(chunk_id, groups=principal.acl_groups())
            if hit is None:
                raise ToolError(f"chunk {chunk_id} not found or not visible to this user")
            return hit

    @mcp.tool(annotations=read_only, meta={"com.mara/scope": "docs:read"})
    async def list_sources(ctx: Context) -> dict[str, Any]:
        """List document types and how many documents of each this user can see."""
        principal = require_scope(SERVER, "list_sources")
        with tool_metrics(SERVER, "list_sources"):
            await apply_fault(ctx, settings)
            return {"sources": await store.sources(groups=principal.acl_groups())}

    return mcp, verifier


def create_app(settings: Settings | None = None, store: KnowledgeStore | None = None):
    settings = settings or get_settings()
    mcp, verifier = create_server(settings, store)
    return asgi_app(mcp, server=SERVER, verifier=verifier, resource=settings.knowledge_resource)
