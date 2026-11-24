"""Tools: list_incidents, get_incident, get_incident_timeline, get_service_status,
search_issues, get_issue, list_pull_requests, get_pull_request, get_fx_rate."""

from __future__ import annotations

from typing import Annotated, Any, Literal

import httpx
from mara_core.mcpserver import (
    EmptyResult,
    apply_fault,
    asgi_app,
    build_server,
    require_scope,
    tool_metrics,
)
from mara_core.settings import Settings, get_settings
from mcp.server.fastmcp import Context
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

SERVER = "live_data"
INSTRUCTIONS = (
    "Live operational data: incidents and their timelines, service status, GitHub issues and pull requests, FX rates."
)


class Upstream:
    """Talks to the mock APIs (default) or real GitHub, with this server's own credentials."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.http = httpx.AsyncClient(base_url=settings.mock_apis_url, timeout=8.0)
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if settings.github_token:
            headers["Authorization"] = f"Bearer {settings.github_token.get_secret_value()}"
        self.github = httpx.AsyncClient(base_url="https://api.github.com", timeout=8.0, headers=headers)

    async def get(self, path: str, **params: Any) -> Any:
        params = {k: v for k, v in params.items() if v is not None}
        try:
            resp = await self.http.get(path, params=params)
        except httpx.TimeoutException as exc:
            raise ToolError("upstream timed out; try again or answer without this source") from exc
        except httpx.HTTPError as exc:
            raise ToolError(f"upstream unreachable: {exc.__class__.__name__}") from exc
        if resp.status_code == 404:
            raise ToolError(resp.json().get("detail", "not found"))
        if resp.status_code >= 400:
            raise ToolError(f"upstream returned HTTP {resp.status_code}")
        return resp.json()

    async def github_get(self, path: str, **params: Any) -> Any:
        if self.settings.live_data_backend == "mock":
            return await self.get(f"/github{path}", **params)
        resp = await self.github.get(path, params={k: v for k, v in params.items() if v is not None})
        if resp.status_code >= 400:
            raise ToolError(f"GitHub returned HTTP {resp.status_code}")
        return resp.json()


def _issue_view(issue: dict) -> dict:
    return {k: issue.get(k) for k in ("repo", "number", "title", "state", "labels", "assignee", "created_at", "body")}


def create_server(settings: Settings | None = None):
    settings = settings or get_settings()
    mcp, verifier = build_server(
        name="mara-live-data", resource=settings.live_data_resource, instructions=INSTRUCTIONS, settings=settings
    )
    up = Upstream(settings)
    ro = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True)

    async def guarded(ctx: Context, tool: str):
        require_scope(SERVER, tool)
        await apply_fault(ctx, settings)

    @mcp.tool(annotations=ro, meta={"com.mara/scope": "incidents:read"})
    async def list_incidents(
        ctx: Context,
        since: Annotated[str | None, Field(description="ISO date or datetime, inclusive")] = None,
        until: Annotated[str | None, Field(description="ISO date or datetime, inclusive")] = None,
        service: Annotated[str | None, Field(description="e.g. payments-api, checkout, api-gateway")] = None,
        severity: Annotated[Literal["sev1", "sev2", "sev3"] | None, Field()] = None,
    ) -> dict[str, Any]:
        """List incidents in a time window, newest first, optionally for one service or severity.
        Returns id, service, severity, title, start and end. Use get_incident for affected customers."""
        with tool_metrics(SERVER, "list_incidents"):
            try:
                await guarded(ctx, "list_incidents")
            except EmptyResult:
                return {"incidents": []}
            if until and len(until) == 10:
                until = until + "T23:59:59Z"
            return {
                "incidents": await up.get("/incidents", since=since, until=until, service=service, severity=severity)
            }

    @mcp.tool(annotations=ro, meta={"com.mara/scope": "incidents:read"})
    async def get_incident(
        ctx: Context, incident_id: Annotated[str, Field(description="e.g. INC-2041")]
    ) -> dict[str, Any]:
        """One incident with its duration, status notes and the ids of affected customers.
        The incident API is the system of record for start, end and duration."""
        with tool_metrics(SERVER, "get_incident"):
            await guarded(ctx, "get_incident")
            return await up.get(f"/incidents/{incident_id}")

    @mcp.tool(annotations=ro, meta={"com.mara/scope": "incidents:read"})
    async def get_incident_timeline(
        ctx: Context, incident_id: Annotated[str, Field(description="e.g. INC-2041")]
    ) -> dict[str, Any]:
        """The timestamped event timeline of one incident (detection, escalation, mitigation, resolution)."""
        with tool_metrics(SERVER, "get_incident_timeline"):
            await guarded(ctx, "get_incident_timeline")
            return {"incident_id": incident_id, "events": await up.get(f"/incidents/{incident_id}/timeline")}

    @mcp.tool(annotations=ro, meta={"com.mara/scope": "incidents:read"})
    async def get_service_status(
        ctx: Context, service: Annotated[str, Field(description="e.g. payments-api")]
    ) -> dict[str, Any]:
        """Current status and the last two weeks of daily status for one service."""
        with tool_metrics(SERVER, "get_service_status"):
            await guarded(ctx, "get_service_status")
            return await up.get(f"/status/{service}")

    @mcp.tool(annotations=ro, meta={"com.mara/scope": "github:read"})
    async def search_issues(
        ctx: Context,
        query: Annotated[str, Field(description="Words to match in title or body; add repo:owner/name to limit")],
        state: Literal["open", "closed", "all"] = "all",
    ) -> dict[str, Any]:
        """Search GitHub issues across Acme Cloud repos. Returns repo, number, title, state and labels."""
        with tool_metrics(SERVER, "search_issues"):
            try:
                await guarded(ctx, "search_issues")
            except EmptyResult:
                return {"total_count": 0, "items": []}
            data = await up.github_get("/search/issues", q=query, state=state)
            return {
                "total_count": data.get("total_count", 0),
                "items": [_issue_view(i) for i in data.get("items", [])][:20],
            }

    @mcp.tool(annotations=ro, meta={"com.mara/scope": "github:read"})
    async def get_issue(
        ctx: Context, repo: Annotated[str, Field(description="owner/name")], number: int
    ) -> dict[str, Any]:
        """One GitHub issue with its body."""
        with tool_metrics(SERVER, "get_issue"):
            await guarded(ctx, "get_issue")
            return _issue_view(await up.github_get(f"/repos/{repo}/issues/{number}"))

    @mcp.tool(annotations=ro, meta={"com.mara/scope": "github:read"})
    async def list_pull_requests(
        ctx: Context,
        repo: Annotated[str, Field(description="owner/name")],
        state: Literal["open", "closed", "all"] = "open",
    ) -> dict[str, Any]:
        """Pull requests in one repo: number, title, state, author and linked incident if any."""
        with tool_metrics(SERVER, "list_pull_requests"):
            await guarded(ctx, "list_pull_requests")
            return {"pulls": await up.github_get(f"/repos/{repo}/pulls", state=state)}

    @mcp.tool(annotations=ro, meta={"com.mara/scope": "github:read"})
    async def get_pull_request(
        ctx: Context, repo: Annotated[str, Field(description="owner/name")], number: int
    ) -> dict[str, Any]:
        """One pull request with its description."""
        with tool_metrics(SERVER, "get_pull_request"):
            await guarded(ctx, "get_pull_request")
            return await up.github_get(f"/repos/{repo}/pulls/{number}")

    @mcp.tool(annotations=ro, meta={"com.mara/scope": "fx:read"})
    async def get_fx_rate(
        ctx: Context,
        base: Annotated[str, Field(description="ISO currency, e.g. USD")] = "USD",
        quote: Annotated[str, Field(description="ISO currency, e.g. EUR")] = "EUR",
        date: Annotated[str | None, Field(description="YYYY-MM-DD; default today")] = None,
    ) -> dict[str, Any]:
        """Exchange rate from base to quote currency for a date."""
        with tool_metrics(SERVER, "get_fx_rate"):
            await guarded(ctx, "get_fx_rate")
            return await up.get("/fx", base=base, quote=quote, date=date)

    return mcp, verifier


def create_app(settings: Settings | None = None):
    settings = settings or get_settings()
    mcp, verifier = create_server(settings)
    return asgi_app(mcp, server=SERVER, verifier=verifier, resource=settings.live_data_resource)
