"""A small client for the orchestrator API, used by the CLI, the eval harness and tests."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
from httpx_sse import aconnect_sse

from mara_core.auth.client import TokenClient
from mara_core.settings import Settings


class MaraClient:
    def __init__(self, settings: Settings, timeout: float = 30.0):
        self.settings = settings
        self.base = settings.orchestrator_url.rstrip("/")
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(timeout, read=None))
        self._login = TokenClient(token_url=settings.auth_token_url, client_id=settings.ui_client_id)

    async def aclose(self) -> None:
        await self.http.aclose()
        await self._login.aclose()

    async def token(self, username: str, password: str | None = None) -> str:
        """Dev login (password grant). The demo realm's passwords equal the usernames."""
        return await self._login.password(username=username, password=password or username)

    @staticmethod
    def _headers(token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    async def _json(self, method: str, path: str, token: str, **kwargs: Any) -> Any:
        resp = await self.http.request(method, self.base + path, headers=self._headers(token), **kwargs)
        if resp.status_code >= 400:
            try:
                detail = resp.json().get("detail")
            except ValueError:
                detail = resp.text
            raise ApiError(resp.status_code, str(detail))
        return resp.json()

    async def ask(self, token: str, question: str, **options: Any) -> str:
        body = {"question": question, **{k: v for k, v in options.items() if v is not None}}
        return (await self._json("POST", "/v1/runs", token, json=body))["run_id"]

    async def run(self, token: str, run_id: str) -> dict:
        return await self._json("GET", f"/v1/runs/{run_id}", token)

    async def runs(self, token: str, limit: int = 20) -> list[dict]:
        return (await self._json("GET", f"/v1/runs?limit={limit}", token))["runs"]

    async def tool_calls(self, token: str, run_id: str) -> list[dict]:
        return (await self._json("GET", f"/v1/runs/{run_id}/tool-calls", token))["tool_calls"]

    async def cancel(self, token: str, run_id: str) -> dict:
        return await self._json("POST", f"/v1/runs/{run_id}/cancel", token)

    async def replay(self, token: str, run_id: str) -> str:
        return (await self._json("POST", f"/v1/runs/{run_id}/replay", token))["run_id"]

    async def approvals(self, token: str, status: str = "pending_approval") -> list[dict]:
        return (await self._json("GET", f"/v1/approvals?status={status}", token))["actions"]

    async def decide(self, token: str, action_id: str, approve: bool, note: str | None = None) -> dict:
        return await self._json(
            "POST",
            f"/v1/approvals/{action_id}",
            token,
            json={"decision": "approve" if approve else "reject", "note": note},
        )

    async def me(self, token: str) -> dict:
        return await self._json("GET", "/v1/me", token)

    async def events(self, token: str, run_id: str, after: int = 0) -> AsyncIterator[dict]:
        """Follow a run's events until the stream closes (after `run_finished`)."""
        url = f"{self.base}/v1/runs/{run_id}/events?after={after}"
        async with aconnect_sse(self.http, "GET", url, headers=self._headers(token)) as source:
            if source.response.status_code >= 400:
                raise ApiError(source.response.status_code, (await source.response.aread()).decode()[:300])
            async for sse in source.aiter_sse():
                if not sse.event or sse.event == "message" and not sse.data:
                    continue
                yield {
                    "id": int(sse.id) if sse.id and sse.id.isdigit() else None,
                    "type": sse.event,
                    "data": json.loads(sse.data) if sse.data else {},
                }


class ApiError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(f"HTTP {status}: {detail}")
        self.status = status
        self.detail = detail
