"""MCP tool-call interceptors (langchain-mcp-adapters' onion-style hooks).

One interceptor per task wraps every MCP call a worker makes:
  - replay: in a replay run, serve the recorded result instead of calling out;
  - faults: in eval runs, add an `x-mara-fault` header the server honours;
  - retries: read-only tools retry timeouts and 5xx a bounded number of times;
  - errors: 401/403/timeouts become clear tool errors the model can react to
    ("not permitted: requires sql:read"), never crashes;
  - evidence: results are masked, scanned, spotlighted and numbered;
  - recording: every call lands in `tool_calls`, which is what replay reads.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any

import httpx
from langchain_mcp_adapters.interceptors import MCPToolCallRequest
from mara_core.hashing import args_hash
from mara_core.logging import get_logger
from mara_core.pii import mask_pii
from mcp.shared.exceptions import McpError
from mcp.types import CallToolResult, TextContent

from mara_orchestrator.tools.evidence import EvidenceBuilder

log = get_logger(__name__)

RETRIES = {"knowledge": 2, "warehouse": 1, "live_data": 2}
TRANSIENT_TOOL_ERROR = re.compile(r"timed out|HTTP 5\d\d|unreachable|temporarily", re.I)


@dataclass
class FaultPlan:
    """Eval-only: which calls should fail and how. Shared by all tasks of one run."""

    rules: list[dict[str, Any]] = field(default_factory=list)
    used: dict[int, int] = field(default_factory=lambda: defaultdict(int))

    def header_for(self, server: str, tool: str) -> str | None:
        for i, rule in enumerate(self.rules):
            if rule.get("server") not in (None, server) or rule.get("tool") not in (None, tool):
                continue
            if self.used[i] < int(rule.get("times", 1)):
                self.used[i] += 1
                return str(rule["fault"])
        return None


class ReplaySource:
    """Recorded tool results for a replay run, consumed in recorded order per key."""

    def __init__(self, rows: list[dict[str, Any]]):
        self._calls: dict[tuple[str, str, str], deque] = defaultdict(deque)
        for row in rows:
            self._calls[(row["server"], row["tool"], row["args_sha256"])].append(row)
        self.diverged: list[dict[str, Any]] = []

    def take(self, server: str, tool: str, args: dict[str, Any]) -> dict[str, Any] | None:
        key = (server, tool, args_hash(args))
        queue = self._calls.get(key)
        if queue:
            return queue.popleft() if len(queue) > 1 else queue[0]
        self.diverged.append({"server": server, "tool": tool, "args": args})
        return None


def _error_result(message: str) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=message)], isError=True)


def _flatten(exc: BaseException) -> BaseException:
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    return exc


def describe_error(exc: BaseException) -> tuple[str, bool]:
    """Return (message for the model, retryable)."""
    inner = _flatten(exc)
    if isinstance(inner, httpx.HTTPStatusError):
        status = inner.response.status_code
        if status == 403:
            challenge = inner.response.headers.get("www-authenticate", "")
            scope = (
                challenge.split('scope="', 1)[1].split('"', 1)[0]
                if 'scope="' in challenge
                else "a scope this task lacks"
            )
            return f"not permitted: this task's token lacks {scope}", False
        if status == 401:
            return "not authorized: the task token was rejected", False
        return f"upstream error: HTTP {status}", status >= 500
    if isinstance(inner, (httpx.TimeoutException, asyncio.TimeoutError, TimeoutError)):
        return "the tool timed out", True
    if isinstance(inner, McpError) and "timed out" in str(inner).lower():
        return "the tool timed out", True
    if isinstance(inner, httpx.HTTPError):
        return f"tool server unreachable ({inner.__class__.__name__})", True
    return f"tool call failed: {str(inner)[:200]}", False


class TaskToolInterceptor:
    def __init__(
        self,
        *,
        run_id: str,
        task_id: str,
        evidence: EvidenceBuilder,
        recorder: Any | None,
        faults: FaultPlan | None = None,
        replay: ReplaySource | None = None,
        emit=None,
    ):
        self.run_id = run_id
        self.emit = emit
        self.task_id = task_id
        self.evidence = evidence
        self.recorder = recorder
        self.faults = faults
        self.replay = replay
        self.seq = 0
        self.errors: dict[str, int] = defaultdict(int)

    async def __call__(self, request: MCPToolCallRequest, handler) -> CallToolResult:
        server, tool, args = request.server_name, request.name, dict(request.args)
        self.seq += 1
        started = time.monotonic()
        if self.replay is not None:
            recorded = self.replay.take(server, tool, args)
            if recorded is None:
                return _error_result("not in recording: this call did not happen in the original run")
            if recorded.get("is_error"):
                await self._notify(server, tool, args, False, started, replayed=True)
                return _error_result(str((recorded.get("result") or {}).get("error", "error")))
            await self._notify(server, tool, args, True, started, replayed=True)
            return await self._finish(server, tool, args, recorded.get("result"), started, record=False)

        attempts = RETRIES.get(server, 0) + 1
        last_message = "tool call failed"
        for attempt in range(attempts):
            req = request
            fault = self.faults.header_for(server, tool) if self.faults else None
            if fault:
                req = request.override(headers={**(request.headers or {}), "x-mara-fault": fault})
            try:
                result = await handler(req)
            except BaseException as exc:  # noqa: BLE001 - MCP client errors arrive as exception groups
                if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt)):
                    raise
                last_message, retryable = describe_error(exc)
                log.info("tool_call_failed", server=server, tool=tool, attempt=attempt + 1, error=last_message)
                if retryable and attempt + 1 < attempts:
                    await asyncio.sleep(0.5 * (2**attempt))
                    continue
                break
            if not isinstance(result, CallToolResult):
                return result
            if result.isError:
                text = " ".join(c.text for c in result.content if isinstance(c, TextContent))
                if TRANSIENT_TOOL_ERROR.search(text) and attempt + 1 < attempts:  # read tools are safe to retry
                    log.info("tool_error_retry", server=server, tool=tool, attempt=attempt + 1, error=text[:120])
                    await asyncio.sleep(0.5 * (2**attempt))
                    continue
                self.errors[tool] += 1
                await self._record(server, tool, args, {"error": text}, True, started)
                return _error_result(text)
            payload = result.structuredContent
            if isinstance(payload, dict) and set(payload) == {"result"}:
                payload = payload["result"]
            if payload is None:
                text = " ".join(c.text for c in result.content if isinstance(c, TextContent))
                try:
                    payload = json.loads(text)
                except json.JSONDecodeError:
                    payload = text
            return await self._finish(server, tool, args, payload, started)
        self.errors[tool] += 1
        await self._record(server, tool, args, {"error": last_message}, True, started)
        return _error_result(last_message)

    async def _finish(
        self, server: str, tool: str, args: dict, payload: Any, started: float, record: bool = True
    ) -> CallToolResult:
        text, _items = self.evidence.from_result(server, tool, args, payload)
        if record:
            await self._record(server, tool, args, payload, False, started)
        return CallToolResult(content=[TextContent(type="text", text=text)], isError=False)

    async def _notify(
        self, server: str, tool: str, args: dict, ok: bool, started: float, replayed: bool = False
    ) -> None:
        if self.emit is None:
            return
        shown, _ = mask_pii(json.dumps(args, ensure_ascii=False, default=str))
        await self.emit(
            "tool_call",
            {
                "task_id": self.task_id,
                "server": server,
                "tool": tool,
                "args": shown[:400],
                "ok": ok,
                "latency_ms": int((time.monotonic() - started) * 1000),
                "replayed": replayed,
            },
        )

    async def _record(self, server: str, tool: str, args: dict, payload: Any, is_error: bool, started: float) -> None:
        await self._notify(server, tool, args, not is_error, started)
        if self.recorder is None:
            return
        try:
            masked, _ = mask_pii(json.dumps(payload, ensure_ascii=False, default=str))
            await self.recorder.record_tool_call(
                run_id=self.run_id,
                task_id=self.task_id,
                seq=self.seq,
                server=server,
                tool=tool,
                args=args,
                args_sha256=args_hash(args),
                result=json.loads(masked),
                is_error=is_error,
                latency_ms=int((time.monotonic() - started) * 1000),
            )
        except Exception as exc:  # recording must never break a run
            log.warning("tool_call_record_failed", error=str(exc)[:200])
