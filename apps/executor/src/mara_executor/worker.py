"""The executor loop."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

import httpx
from mara_core import audit
from mara_core.actions import InvalidAction, action_hash
from mara_core.auth.approval import ApprovalError, ApprovalVerifier
from mara_core.auth.client import TokenClient
from mara_core.config import scope_policy
from mara_core.db import fetch_one, make_pool
from mara_core.logging import get_logger
from mara_core.paths import resolve
from mara_core.settings import Settings
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.types import CallToolResult, TextContent
from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

log = get_logger(__name__)
CHANNEL = "mara_actions"
MAX_ATTEMPTS = 3
POLL_SECONDS = 5.0
APPROVAL_META = "com.mara/approval"
IDEMPOTENCY_META = "com.mara/idempotency_key"


class Transient(Exception):
    """Worth retrying: the server or network had a problem, the request itself was fine."""


def _flatten(exc: BaseException) -> BaseException:
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    return exc


class Executor:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.pool = make_pool(settings.app_db_url, max_size=4, name="executor")
        secret = settings.executor_client_secret.get_secret_value() if settings.executor_client_secret else None
        self.tokens = TokenClient(
            token_url=settings.auth_token_url,
            client_id=settings.executor_client_id,
            client_secret=secret,
            private_key_file=settings.executor_client_key_file,
        )
        self.approvals = ApprovalVerifier(str(resolve(settings.approval_public_key_file)))
        self._wake = asyncio.Event()

    # ------------------------------------------------------------ database
    async def claim(self) -> dict | None:
        return await fetch_one(
            self.pool,
            """
            update actions set status = 'executing', attempts = attempts + 1, updated_at = now()
            where id = (select id from actions where status = 'approved' order by updated_at
                        for update skip locked limit 1)
            returning id, run_id, tool, args, args_sha256, approval_token, idempotency_key, attempts, approver""",
        )

    async def finish(self, action: dict, *, status: str, result: Any = None, error: str | None = None) -> None:
        clear = status in ("executed", "failed")
        async with self.pool.connection() as conn, conn.transaction():
            await conn.execute(
                "update actions set status = %s, result = %s, error = %s, updated_at = now(), "
                "executed_at = case when %s = 'executed' then now() else executed_at end, "
                "approval_token = case when %s then null else approval_token end where id = %s",
                (status, Jsonb(result) if result is not None else None, error, status, clear, action["id"]),
            )
            if clear:
                await conn.execute(
                    "insert into run_events (run_id, type, data) values (%s, %s, %s)",
                    (
                        action["run_id"],
                        f"action_{status}",
                        Jsonb({"action_id": action["id"], "tool": action["tool"], "result": result, "error": error}),
                    ),
                )
        if clear:
            await audit.append(
                self.pool,
                event=f"action_{status}",
                actor="executor",
                run_id=action["run_id"],
                data={
                    "action_id": action["id"],
                    "tool": action["tool"],
                    "error": error,
                    "args_sha256": action["args_sha256"],
                },
            )

    # ---------------------------------------------------------------- work
    async def call_tool(self, action: dict) -> dict:
        scope = scope_policy().scope_for_tool("actions", action["tool"])
        if not scope:
            raise ValueError(f"no scope configured for {action['tool']}")
        token = await self.tokens.client_credentials(audience=self.settings.actions_resource, scopes=[scope])
        meta = {APPROVAL_META: action["approval_token"], IDEMPOTENCY_META: action["idempotency_key"]}
        try:
            async with (
                streamablehttp_client(
                    self.settings.mcp_actions_url,
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=15,
                    sse_read_timeout=30,
                ) as (read, write, _),
                ClientSession(read, write) as session,
            ):
                await session.initialize()
                result: CallToolResult = await session.call_tool(action["tool"], action["args"], meta=meta)
        except BaseException as exc:  # noqa: BLE001 - the MCP client raises exception groups
            if isinstance(exc, asyncio.CancelledError):
                raise
            inner = _flatten(exc)
            if isinstance(inner, httpx.HTTPStatusError) and inner.response.status_code < 500:
                raise ValueError(f"actions server refused the call: HTTP {inner.response.status_code}") from inner
            raise Transient(f"{inner.__class__.__name__}: {inner}"[:300]) from inner
        text = " ".join(c.text for c in result.content if isinstance(c, TextContent))
        if result.isError:
            raise ValueError(text[:500] or "the actions server returned an error")
        payload = result.structuredContent or {"text": text}
        return payload.get("result", payload) if set(payload) == {"result"} else payload

    async def handle(self, action: dict) -> None:
        log.info("action_claimed", action_id=action["id"], tool=action["tool"], attempt=action["attempts"])
        # Re-check the approval here too: a row edited in the database cannot slip through.
        try:
            if not action.get("approval_token"):
                raise ApprovalError("no approval token")
            self.approvals.verify(
                action["approval_token"],
                tool=action["tool"],
                args_sha256=action_hash(action["tool"], action["args"]),
                action_id=action["id"],
            )
        except (ApprovalError, InvalidAction) as exc:
            await self.finish(action, status="failed", error=f"approval check failed: {exc}")
            return
        try:
            result = await self.call_tool(action)
        except Transient as exc:
            if action["attempts"] < MAX_ATTEMPTS:
                log.warning("action_retry", action_id=action["id"], error=str(exc))
                await asyncio.sleep(2 ** action["attempts"])
                await self.finish(action, status="approved")  # back in the queue
            else:
                await self.finish(action, status="failed", error=f"gave up after {MAX_ATTEMPTS} attempts: {exc}")
            return
        except Exception as exc:
            await self.finish(action, status="failed", error=str(exc)[:500])
            return
        await self.finish(action, status="executed", result=result)
        log.info("action_executed", action_id=action["id"], tool=action["tool"])

    async def drain(self) -> int:
        done = 0
        while (action := await self.claim()) is not None:
            await self.handle(action)
            done += 1
        return done

    # ---------------------------------------------------------------- loop
    async def _listen(self) -> None:
        backoff = 1.0
        while True:
            try:
                async with await AsyncConnection.connect(self.settings.app_db_url, autocommit=True) as conn:
                    await conn.execute(f"LISTEN {CHANNEL}")
                    backoff = 1.0
                    async for _ in conn.notifies():
                        self._wake.set()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("executor_listen_reconnecting", error=str(exc)[:200])
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    async def run_forever(self) -> None:
        await self.pool.open(wait=True, timeout=60)
        listener = asyncio.create_task(self._listen())
        log.info("executor_ready", actions_url=self.settings.mcp_actions_url)
        try:
            while True:
                try:
                    await self.drain()
                except Exception as exc:
                    log.exception("executor_loop_error", error=str(exc)[:200])
                self._wake.clear()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), timeout=POLL_SECONDS)
        finally:
            listener.cancel()
            await self.tokens.aclose()
            await self.pool.close()
