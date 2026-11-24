"""Turn raw tool results into evidence: masked, scanned, labelled and spotlighted.

Every fact the final answer states must trace to an evidence item. A tool
result becomes one or more items, each with an id (`ev_t1_3`), a source, a URI
and its text with personal data masked. The worker's model never sees the raw
result; it sees the items wrapped in <untrusted> elements carrying their ids,
which is how it learns what to cite.
"""

from __future__ import annotations

import json
from typing import Any

from mara_core import injection, spotlight
from mara_core.pii import mask_pii

SOURCE_OF = {"knowledge": "doc", "warehouse": "sql", "live_data": "api"}
PASSTHROUGH = {("knowledge", "list_sources"), ("warehouse", "list_tables"), ("warehouse", "describe_table")}
MAX_ROWS_IN_PROMPT = 30


def _render_rows(columns: list[str], rows: list[dict]) -> str:
    lines = [" | ".join(columns)]
    for row in rows[:MAX_ROWS_IN_PROMPT]:
        lines.append(" | ".join(str(row.get(c, "")) for c in columns))
    if len(rows) > MAX_ROWS_IN_PROMPT:
        lines.append(f"... {len(rows) - MAX_ROWS_IN_PROMPT} more rows")
    return "\n".join(lines)


class EvidenceBuilder:
    """Numbers evidence per task, so parallel workers never collide on ids."""

    def __init__(self, task_id: str):
        self.task_id = task_id
        self.counter = 0
        self.items: list[dict[str, Any]] = []

    def _new(
        self,
        *,
        server: str,
        tool: str,
        uri: str,
        title: str,
        text: str,
        data: dict | None = None,
        created_at: str | None = None,
        source: str | None = None,
    ) -> dict[str, Any]:
        self.counter += 1
        masked, pii_count = mask_pii(text)
        verdict = injection.score(masked)
        item = {
            "id": f"ev_{self.task_id}_{self.counter}",
            "task_id": self.task_id,
            "source": source or SOURCE_OF.get(server, server),
            "server": server,
            "tool": tool,
            "uri": uri,
            "title": title,
            "created_at": created_at,
            "content": masked[:1500],
            "data": data or {},
            "trusted": False,
            "flagged": verdict.flagged,
            "injection_score": verdict.score,
            "pii_masked": pii_count,
        }
        self.items.append(item)
        return item

    def from_result(self, server: str, tool: str, args: dict[str, Any], payload: Any) -> tuple[str, list[dict]]:
        """Return (text for the model, new evidence items)."""
        if (server, tool) in PASSTHROUGH:
            text = json.dumps(payload, ensure_ascii=False) if not isinstance(payload, str) else payload
            masked, _ = mask_pii(text)
            return masked[:4000], []
        start = len(self.items)
        if server == "knowledge" and tool == "search_docs":
            for hit in (payload or {}).get("hits", []):
                self._new(
                    server=server,
                    tool=tool,
                    uri=f"doc://{hit.get('doc_id')}#{hit.get('chunk_id')}",
                    title=f"{hit.get('title')} ({hit.get('section') or 'body'})",
                    text=hit.get("text", ""),
                    created_at=hit.get("created_at"),
                    data={"chunk_id": hit.get("chunk_id"), "doc_id": hit.get("doc_id")},
                )
        elif server == "knowledge" and tool == "fetch_chunk":
            hit = payload or {}
            self._new(
                server=server,
                tool=tool,
                uri=f"doc://{hit.get('doc_id')}#{hit.get('chunk_id')}",
                title=str(hit.get("title")),
                text=hit.get("text", ""),
                created_at=hit.get("created_at"),
            )
        elif server == "warehouse" and tool == "run_sql":
            result = payload or {}
            columns, rows = result.get("columns", []), result.get("rows", [])
            body = _render_rows(columns, rows) if columns else "(no rows)"
            if result.get("truncated"):
                body += "\n(more rows available: pass result_id to page)"
            self._new(
                server=server,
                tool=tool,
                uri=f"sql://{result.get('query_hash', 'query')}",
                title=f"SQL result ({result.get('row_count', 0)} rows)",
                text=f"SQL: {args.get('sql', '')}\n{body}",
                data={"columns": columns, "rows": rows[:50], "sql": args.get("sql")},
            )
        else:
            text = json.dumps(payload, ensure_ascii=False, default=str)
            arg_text = ", ".join(f"{k}={v}" for k, v in args.items())
            self._new(
                server=server,
                tool=tool,
                uri=f"api://{tool}?{arg_text}",
                title=f"{tool}({arg_text})",
                text=text,
                data=payload if isinstance(payload, dict) else {"items": payload},
            )
        new = self.items[start:]
        if not new:
            return "The tool returned no results.", []
        return self.render(new), new

    def from_brief(self, brief: dict[str, Any], agent_task_id: str | None) -> list[dict]:
        """A research brief from the remote agent: one item per source, plus its summary.

        The remote agent read the open web, so all of this is untrusted; it gets
        the same masking, injection scan and spotlighting as any tool result.
        """
        start = len(self.items)
        claims = brief.get("claims") or []
        for src in (brief.get("sources") or [])[:8]:
            url = str(src.get("url") or "")
            title = str(src.get("title") or url)
            supporting = [str(c.get("text", "")) for c in claims if url in (c.get("source_urls") or [])]
            text = title + ("\n" + "\n".join(f"- {t}" for t in supporting) if supporting else "")
            self._new(
                server="research_agent",
                tool="research",
                uri=url,
                title=title,
                text=text,
                created_at=src.get("published_at"),
                source="web",
            )
        if brief.get("summary"):
            self._new(
                server="research_agent",
                tool="research",
                uri=f"a2a://web-research-agent/tasks/{agent_task_id}",
                title="Research brief summary",
                text=str(brief["summary"]),
                source="web",
            )
        return self.items[start:]

    @staticmethod
    def render(items: list[dict]) -> str:
        return "\n\n".join(spotlight.wrap(i["content"], i["id"], i["source"], i["uri"], i["flagged"]) for i in items)
