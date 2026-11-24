"""Helpers every MCP server in this repo shares.

Each server is an OAuth 2.1 resource server, as the MCP authorization spec asks:

- FastMCP's bearer middleware rejects requests without a valid token for this
  server (signature, issuer, expiry, audience) with a 401 and a
  `WWW-Authenticate` header pointing at the protected-resource metadata.
- `ScopeGuard` checks the scope a tool needs *before* the call reaches the tool,
  and answers `403` with `WWW-Authenticate: Bearer error="insufficient_scope"`.
- `require_scope` repeats that check inside the tool, so a tool can never run
  without its scope even if the middleware is misconfigured.

`tools/list` only needs a valid token for this audience; the orchestrator uses
a `catalog:read` token for it. Calling a tool always needs that tool's scope.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from mara_core.auth.verify import AuthError, JWTVerifier, Principal, bearer_from_header
from mara_core.config import scope_policy
from mara_core.logging import get_logger
from mara_core.settings import Settings, get_settings

log = get_logger(__name__)

TOOL_CALLS = Counter("mara_mcp_tool_calls_total", "MCP tool calls", ["server", "tool", "outcome"])
TOOL_LATENCY = Histogram("mara_mcp_tool_latency_seconds", "MCP tool latency", ["server", "tool"])
SCOPE_DENIALS = Counter(
    "mara_scope_denials_total", "Requests refused for scope or audience", ["server", "tool", "reason"]
)


class JWTTokenVerifier:
    """Adapts our JWT checks to the MCP SDK's TokenVerifier protocol."""

    def __init__(self, verifier: JWTVerifier, resource: str):
        self.verifier = verifier
        self.resource = resource

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            principal = await self.verifier.averify(token, audience=self.resource)
        except AuthError as exc:
            log.info("mcp_token_rejected", resource=self.resource, reason=str(exc))
            return None
        return AccessToken(
            token=token,
            client_id=principal.client_id or principal.subject,
            scopes=sorted(principal.scopes),
            expires_at=principal.expires_at,
            resource=self.resource,
            subject=principal.subject,
            claims=principal.claims,
        )


def current_principal() -> Principal:
    token = get_access_token()
    if token is None:
        raise ToolError("unauthenticated")
    return Principal.from_claims(
        token.claims or {"sub": token.subject or token.client_id, "scope": " ".join(token.scopes)}
    )


def require_scope(server: str, tool: str) -> Principal:
    """Raise a tool error unless the caller's token carries the tool's scope."""
    principal = current_principal()
    needed = scope_policy().scope_for_tool(server, tool)
    if needed is None or not principal.has_scope(needed):
        SCOPE_DENIALS.labels(server, tool, "scope").inc()
        raise ToolError(f"insufficient_scope: {tool} requires {needed}")
    return principal


class ScopeGuard:
    """ASGI middleware: refuse `tools/call` with 403 when the token lacks the tool's scope."""

    def __init__(self, app: ASGIApp, *, server: str, verifier: JWTVerifier, resource: str, metadata_url: str):
        self.app = app
        self.server = server
        self.verifier = verifier
        self.resource = resource
        self.metadata_url = metadata_url

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] != "POST":
            await self.app(scope, receive, send)
            return
        body = b""
        more = True
        while more:
            message = await receive()
            body += message.get("body", b"")
            more = message.get("more_body", False)

        tool = _tool_name(body)
        if tool is not None:
            needed = scope_policy().scope_for_tool(self.server, tool)
            headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
            try:
                principal = await self.verifier.averify(
                    bearer_from_header(headers.get("authorization")), audience=self.resource
                )
            except AuthError:
                principal = None  # FastMCP's bearer middleware answers 401 below
            if principal is not None and (needed is None or not principal.has_scope(needed)):
                SCOPE_DENIALS.labels(self.server, tool, "scope").inc()
                log.info("scope_denied", server=self.server, tool=tool, needed=needed, client=principal.client_id)
                response = JSONResponse(
                    {"error": "insufficient_scope", "error_description": f"{tool} requires {needed}"},
                    status_code=403,
                    headers={
                        "WWW-Authenticate": (
                            f'Bearer error="insufficient_scope", scope="{needed}", '
                            f'resource_metadata="{self.metadata_url}"'
                        )
                    },
                )
                await response(scope, _replay(b""), send)
                return
        await self.app(scope, _replay(body), send)


def _tool_name(body: bytes) -> str | None:
    try:
        message = json.loads(body or b"null")
    except json.JSONDecodeError:
        return None
    if isinstance(message, dict) and message.get("method") == "tools/call":
        return (message.get("params") or {}).get("name")
    return None


def _replay(body: bytes) -> Receive:
    sent = False

    async def receive() -> dict[str, Any]:
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    return receive


# ---------------------------------------------------------------------- faults
async def apply_fault(ctx: Any, settings: Settings | None = None) -> None:
    """Simulate failures in eval runs.

    The orchestrator's eval harness sets an `x-mara-fault` header on chosen tool
    calls (`timeout`, `error`, `empty`, `latency=1500`). Servers only honour it
    when ENABLE_FAULT_INJECTION is on, which it never is in production.
    """
    settings = settings or get_settings()
    if not settings.enable_fault_injection:
        return
    try:
        request: Request = ctx.request_context.request
        fault = request.headers.get("x-mara-fault", "")
    except Exception:
        return
    if not fault:
        return
    if fault == "timeout":
        await asyncio.sleep(30)
    if fault.startswith("latency="):
        await asyncio.sleep(int(fault.split("=", 1)[1]) / 1000)
    if fault == "error":
        raise ToolError("upstream returned HTTP 500 (injected fault)")
    if fault == "empty":
        raise _EmptyResult()


class _EmptyResult(Exception):
    """Raised by apply_fault('empty'); tools catch it and return an empty result."""


EmptyResult = _EmptyResult


# --------------------------------------------------------------------- builder
def build_server(
    *,
    name: str,
    resource: str,
    instructions: str,
    settings: Settings | None = None,
) -> tuple[FastMCP, JWTVerifier]:
    """Create a FastMCP server that requires a valid token for `resource`."""
    settings = settings or get_settings()
    verifier = JWTVerifier(
        jwks_url=settings.auth_jwks_url,
        issuer=settings.auth_issuer,
        audience=resource,
        leeway=settings.auth_leeway_seconds,
    )
    mcp = FastMCP(
        name=name,
        instructions=instructions,
        token_verifier=JWTTokenVerifier(verifier, resource),
        auth=AuthSettings(
            issuer_url=settings.auth_issuer,
            resource_server_url=resource,
            required_scopes=[],
            validate_token_resource=True,
        ),
        host=settings.host,
        stateless_http=True,
        json_response=True,
        # Tokens, not cookies, protect these servers, so DNS-rebinding checks on
        # the Host header add nothing and would block in-cluster hostnames.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    @mcp.custom_route("/healthz", methods=["GET"])
    async def healthz(_: Request) -> Response:
        return PlainTextResponse("ok")

    @mcp.custom_route("/metrics", methods=["GET"])
    async def metrics(_: Request) -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    return mcp, verifier


def asgi_app(mcp: FastMCP, *, server: str, verifier: JWTVerifier, resource: str) -> ASGIApp:
    from mcp.server.auth.routes import build_resource_metadata_url

    inner = mcp.streamable_http_app()
    return ScopeGuard(
        inner,
        server=server,
        verifier=verifier,
        resource=resource,
        metadata_url=str(build_resource_metadata_url(resource)),
    )


class tool_metrics:
    """Context manager: count and time one tool call.

    A context manager rather than a decorator, because FastMCP builds each tool's
    JSON schema from the function signature and decorators tend to hide it.
    """

    def __init__(self, server: str, tool: str):
        self.server, self.tool = server, tool

    def __enter__(self) -> tool_metrics:
        self._timer = TOOL_LATENCY.labels(self.server, self.tool).time()
        self._timer.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self._timer.__exit__(exc_type, exc, tb)
        TOOL_CALLS.labels(self.server, self.tool, "error" if exc_type else "ok").inc()
