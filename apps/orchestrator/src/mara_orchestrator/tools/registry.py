"""The tool registry: every read tool the platform may use, pinned and searchable.

Built from each read server's `tools/list` (with a catalog-only token) at
startup and whenever a server's catalog goes stale. Each tool is hashed
(name, description, input schema, scope) and compared with
`mcp-tools.lock.json`. A tool whose hash is missing or different is disabled:
a server cannot quietly rewrite a description to steer the model ("tool
poisoning"). Run `uv run mara tools lock` after reviewing a deliberate change.

`search()` backs the `find_tools` meta-tool. It ranks tool names and
descriptions with BM25 and only ever returns tools inside the task's scopes.
"""

from __future__ import annotations

import json
import math
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mara_core.config import scope_policy
from mara_core.hashing import canonical_json, sha256_hex
from mara_core.logging import get_logger
from mara_core.paths import repo_root
from mara_core.retrieval.sparse import tokenize
from mara_core.settings import Settings
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.types import Tool

log = get_logger(__name__)

READ_SERVERS = ("knowledge", "warehouse", "live_data")
TOOLSET_SUMMARY = {
    "knowledge": "docs: postmortems, runbooks and product docs (search_docs, fetch_chunk, list_sources)",
    "warehouse": "sql: customers, ARR, subscriptions, invoices, incidents and impacts, daily usage (read-only SQL)",
    "live_data": "live_api: incidents and timelines, service status, GitHub issues and pull requests, FX rates",
    "research_agent": "web_research: a remote agent that researches the public web and returns a cited brief",
}


@dataclass
class ToolCard:
    server: str
    name: str
    description: str
    input_schema: dict[str, Any]
    scope: str | None
    pin_hash: str
    definition: Tool
    enabled: bool = True
    tokens: list[str] = field(default_factory=list)


def pin_hash(server: str, tool: Tool) -> str:
    scope = (tool.meta or {}).get("com.mara/scope")
    return sha256_hex(
        canonical_json(
            {
                "server": server,
                "name": tool.name,
                "description": tool.description or "",
                "input_schema": tool.inputSchema,
                "scope": scope,
            }
        )
    )


def lock_path() -> Path:
    return repo_root() / "mcp-tools.lock.json"


def load_lock() -> dict[str, dict[str, str]] | None:
    path = lock_path()
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8")).get("servers", {})


async def list_server_tools(url: str, token: str) -> list[Tool]:
    async with (
        streamablehttp_client(url, headers={"Authorization": f"Bearer {token}"}, timeout=10, sse_read_timeout=10) as (
            read,
            write,
            _,
        ),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        result = await session.list_tools()
        return list(result.tools)


class ToolRegistry:
    def __init__(self, settings: Settings, broker: Any, ttl_seconds: float = 300.0):
        self.settings = settings
        self.broker = broker
        self.ttl = ttl_seconds
        self.cards: dict[str, dict[str, ToolCard]] = {}
        self._loaded_at: dict[str, float] = {}
        self._doc_freq: Counter[str] = Counter()

    async def refresh(self, servers: tuple[str, ...] = READ_SERVERS, *, strict: bool | None = None) -> None:
        strict = self.settings.environment == "prod" if strict is None else strict
        lock = load_lock()
        if lock is None:
            if strict:
                raise RuntimeError("mcp-tools.lock.json is missing; run `uv run mara tools lock`")
            log.warning("tool_lock_missing", hint="run `uv run mara tools lock`; tools are unpinned until then")
        for server in servers:
            token = await self.broker.catalog_token(server)
            try:
                tools = await list_server_tools(self.settings.mcp_url_for(server), token)
            except Exception as exc:
                log.error("tool_catalog_unavailable", server=server, error=str(exc)[:200])
                continue
            cards = {}
            policy = scope_policy()
            for tool in tools:
                h = pin_hash(server, tool)
                pinned = lock.get(server, {}).get(tool.name) if lock is not None else h
                enabled = pinned == h
                if not enabled:
                    log.error(
                        "tool_pin_mismatch",
                        server=server,
                        tool=tool.name,
                        hint="description or schema changed; review and run `mara tools lock`",
                    )
                declared = (tool.meta or {}).get("com.mara/scope")
                expected = policy.scope_for_tool(server, tool.name)
                if declared != expected:
                    log.error("tool_scope_mismatch", server=server, tool=tool.name, declared=declared, policy=expected)
                    enabled = False
                cards[tool.name] = ToolCard(
                    server,
                    tool.name,
                    tool.description or "",
                    tool.inputSchema,
                    expected,
                    h,
                    tool,
                    enabled,
                    tokenize(f"{tool.name.replace('_', ' ')} {tool.description or ''}"),
                )
            self.cards[server] = cards
            self._loaded_at[server] = time.monotonic()
            log.info(
                "tool_catalog_loaded",
                server=server,
                tools=len(cards),
                disabled=sum(not c.enabled for c in cards.values()),
            )
        self._doc_freq = Counter(t for cards in self.cards.values() for c in cards.values() for t in set(c.tokens))

    async def ensure_fresh(self) -> None:
        stale = tuple(s for s in READ_SERVERS if time.monotonic() - self._loaded_at.get(s, 0) > self.ttl)
        if stale:
            await self.refresh(stale)

    def cards_for(self, server: str, scopes: list[str]) -> list[ToolCard]:
        return [c for c in self.cards.get(server, {}).values() if c.enabled and c.scope in scopes]

    def search(
        self, query: str, server: str, scopes: list[str], k: int = 3, exclude: set[str] | None = None
    ) -> list[ToolCard]:
        exclude = exclude or set()
        candidates = [c for c in self.cards_for(server, scopes) if c.name not in exclude]
        q = tokenize(query)
        n = max(sum(len(v) for v in self.cards.values()), 1)
        scored = []
        for card in candidates:
            tf = Counter(card.tokens)
            score = 0.0
            for term in q:
                if term in tf:
                    idf = math.log(1 + (n - self._doc_freq[term] + 0.5) / (self._doc_freq[term] + 0.5))
                    score += idf * tf[term] * 2.2 / (tf[term] + 1.2 * (0.25 + 0.75 * len(card.tokens) / 20))
            if score > 0:
                scored.append((score, card))
        scored.sort(key=lambda x: -x[0])
        return [c for _, c in scored[:k]]

    def toolset_summaries(self) -> str:
        return "\n".join(f"- {line}" for line in TOOLSET_SUMMARY.values())

    def lock_entries(self) -> dict[str, dict[str, str]]:
        return {
            server: {name: card.pin_hash for name, card in sorted(cards.items())}
            for server, cards in sorted(self.cards.items())
        }


def write_lock(entries: dict[str, dict[str, str]]) -> Path:
    path = lock_path()
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "note": "SHA-256 of each tool's name, description, input schema and scope. "
                "Regenerate with `uv run mara tools lock` after reviewing a change.",
                "servers": entries,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return path
