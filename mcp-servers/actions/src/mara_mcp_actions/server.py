"""Tools: create_github_issue, post_slack_message, send_email."""

from __future__ import annotations

import asyncio
from typing import Annotated, Any

from mara_core.actions import InvalidAction, action_hash
from mara_core.auth.approval import ApprovalError, ApprovalVerifier
from mara_core.config import allowlists
from mara_core.logging import get_logger
from mara_core.mcpserver import asgi_app, build_server, require_scope, tool_metrics
from mara_core.paths import data_dir
from mara_core.pii import contains_pii
from mara_core.settings import Settings, get_settings
from mcp.server.fastmcp import Context
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from mara_mcp_actions.backends import Backends
from mara_mcp_actions.state import ActionState

log = get_logger(__name__)
SERVER = "actions"
APPROVAL_META = "com.mara/approval"
IDEMPOTENCY_META = "com.mara/idempotency_key"
INSTRUCTIONS = "Write actions. Only the executor may call these, and only with an approval token."


def _meta(ctx: Context) -> dict[str, Any]:
    meta = ctx.request_context.meta
    if meta is None:
        return {}
    return meta.model_dump(by_alias=True, exclude_none=True)


def create_server(settings: Settings | None = None, state: ActionState | None = None, backends: Backends | None = None):
    settings = settings or get_settings()
    mcp, verifier = build_server(
        name="mara-actions", resource=settings.actions_resource, instructions=INSTRUCTIONS, settings=settings
    )
    approvals = ApprovalVerifier(settings.approval_public_key_file)
    state = state or ActionState(settings.actions_state_db or str(data_dir() / "actions_state.sqlite3"))
    backends = backends or Backends(settings)
    write = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True)

    async def authorize(ctx: Context, tool: str, args: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
        principal = require_scope(SERVER, tool)
        if principal.client_id != settings.executor_client_id:
            raise ToolError("only the executor may call write tools")
        meta = _meta(ctx)
        key = meta.get(IDEMPOTENCY_META)
        if not key:
            raise ToolError(f"missing {IDEMPOTENCY_META}")
        previous = state.get_result(key)
        if previous is not None:
            return key, {**previous, "created": False}
        token = meta.get(APPROVAL_META)
        if not token:
            raise ToolError(f"missing {APPROVAL_META}")
        try:
            claims = approvals.verify(token, tool=tool, args_sha256=action_hash(tool, args))
        except InvalidAction as exc:
            raise ToolError(f"invalid arguments: {exc}") from exc
        except ApprovalError as exc:
            log.warning("approval_rejected", tool=tool, reason=str(exc))
            raise ToolError(f"approval rejected: {exc}") from exc
        if not state.consume_approval(claims.jti, claims.action_id):
            raise ToolError("approval token was already used")
        log.info(
            "approval_accepted", tool=tool, action_id=claims.action_id, run_id=claims.run_id, approver=claims.approver
        )
        return key, None

    @mcp.tool(annotations=write, meta={"com.mara/scope": "github:issues:write"})
    async def create_github_issue(
        ctx: Context,
        repo: Annotated[str, Field(description="owner/name; must be on the allowlist")],
        title: Annotated[str, Field(max_length=120)],
        body: Annotated[str, Field(max_length=4000)] = "",
        labels: list[str] | None = None,
        assignee: Annotated[str | None, Field(pattern=r"^@?[A-Za-z0-9-]{1,39}$")] = None,
    ) -> dict[str, Any]:
        """Open a GitHub issue in an allowlisted repo."""
        args = {"repo": repo, "title": title, "body": body, "labels": labels or [], "assignee": assignee}
        with tool_metrics(SERVER, "create_github_issue"):
            key, previous = await authorize(ctx, "create_github_issue", args)
            if previous:
                return previous
            if repo not in allowlists().github_repos:
                raise ToolError(f"repo {repo} is not on the allowlist")
            if contains_pii(title + " " + body):
                raise ToolError("issue text contains personal data; remove it")
            result = await backends.create_issue(
                repo, title, body, labels or [], assignee.lstrip("@") if assignee else None
            )
            result["created"] = True
            state.save_result(key, "create_github_issue", result)
            return result

    @mcp.tool(annotations=write, meta={"com.mara/scope": "slack:write"})
    async def post_slack_message(
        ctx: Context,
        channel: Annotated[str, Field(description="#channel; must be on the allowlist")],
        text: Annotated[str, Field(max_length=3000)],
    ) -> dict[str, Any]:
        """Post a message to an allowlisted internal Slack channel."""
        args = {"channel": channel, "text": text}
        with tool_metrics(SERVER, "post_slack_message"):
            key, previous = await authorize(ctx, "post_slack_message", args)
            if previous:
                return previous
            if channel not in allowlists().slack_channels:
                raise ToolError(f"channel {channel} is not on the allowlist")
            if contains_pii(text):
                raise ToolError("message contains personal data; remove it")
            result = await backends.post_slack(channel, text)
            result["created"] = True
            state.save_result(key, "post_slack_message", result)
            return result

    @mcp.tool(
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True),
        meta={"com.mara/scope": "email:send"},
    )
    async def send_email(
        ctx: Context,
        to: Annotated[str, Field(description="Recipient; domain must be on the allowlist")],
        subject: Annotated[str, Field(max_length=200)],
        body: Annotated[str, Field(max_length=4000)],
    ) -> dict[str, Any]:
        """Send an email. Disabled unless the allowlist enables email."""
        args = {"to": to, "subject": subject, "body": body}
        with tool_metrics(SERVER, "send_email"):
            if not allowlists().email_enabled:
                raise ToolError("email is disabled in this deployment")
            key, previous = await authorize(ctx, "send_email", args)
            if previous:
                return previous
            domain = to.rsplit("@", 1)[-1].lower()
            if domain not in allowlists().email_domains:
                raise ToolError(f"domain {domain} is not on the allowlist")
            result = await asyncio.to_thread(backends.send_email, to, subject, body)
            result["created"] = True
            state.save_result(key, "send_email", result)
            return result

    return mcp, verifier


def create_app(settings: Settings | None = None, state: ActionState | None = None, backends: Backends | None = None):
    settings = settings or get_settings()
    mcp, verifier = create_server(settings, state, backends)
    return asgi_app(mcp, server=SERVER, verifier=verifier, resource=settings.actions_resource)
