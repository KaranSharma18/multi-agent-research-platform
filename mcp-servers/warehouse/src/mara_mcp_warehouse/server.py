"""Tools: list_tables, describe_table, run_sql."""

from __future__ import annotations

import base64
import datetime as dt
import decimal
import hashlib
import hmac
import json
from typing import Annotated, Any

from mara_core.db import make_pool
from mara_core.logging import get_logger
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
from psycopg_pool import AsyncConnectionPool
from pydantic import Field

from mara_mcp_warehouse.guard import SQLRejected, check_sql

log = get_logger(__name__)
SERVER = "warehouse"

INSTRUCTIONS = (
    "Read-only SQL over the analytics schema: customers, subscriptions, invoices, incidents, "
    "incident_impacts, usage_daily. Call list_tables, then describe_table, then run_sql."
)


def _jsonable(value: Any) -> Any:
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    return value


class Warehouse:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.pool: AsyncConnectionPool = make_pool(settings.warehouse_ro_url, max_size=10, name="warehouse")
        self._opened = False
        self._views: dict[str, dict] | None = None

    async def open(self) -> None:
        if not self._opened:
            await self.pool.open()
            self._opened = True

    async def views(self) -> dict[str, dict]:
        if self._views is None:
            await self.open()
            async with self.pool.connection() as conn:
                cur = await conn.execute(
                    """
                    select c.relname as name, obj_description(c.oid) as description
                    from pg_class c join pg_namespace n on n.oid = c.relnamespace
                    where n.nspname = 'analytics' and c.relkind in ('v', 'r', 'm')
                    order by c.relname
                    """
                )
                self._views = {r["name"]: {"description": r["description"] or ""} for r in await cur.fetchall()}
        return self._views

    def sign_handle(self, sql: str, offset: int) -> str:
        payload = json.dumps({"sql": sql, "offset": offset}).encode()
        mac = hmac.new(
            self.settings.result_handle_secret.get_secret_value().encode(), payload, hashlib.sha256
        ).digest()[:12]
        return base64.urlsafe_b64encode(payload + mac).decode()

    def open_handle(self, handle: str) -> tuple[str, int]:
        try:
            raw = base64.urlsafe_b64decode(handle.encode())
            payload, mac = raw[:-12], raw[-12:]
            expected = hmac.new(
                self.settings.result_handle_secret.get_secret_value().encode(), payload, hashlib.sha256
            ).digest()[:12]
            if not hmac.compare_digest(mac, expected):
                raise ValueError("bad signature")
            data = json.loads(payload)
            return data["sql"], int(data["offset"])
        except Exception as exc:
            raise ToolError("result_id is invalid or was tampered with") from exc

    async def query(self, sql: str, regions: list[str], *, limit: int, offset: int = 0) -> dict[str, Any]:
        await self.open()
        async with self.pool.connection() as conn, conn.transaction():
            await conn.execute("set transaction read only")
            await conn.execute("select set_config('statement_timeout', '5000', true)")
            await conn.execute("select set_config('app.regions', %s, true)", (",".join(regions),))
            cur = await conn.execute(f"select * from ({sql}) as q limit %s offset %s", (limit + 1, offset))
            rows = await cur.fetchall()
            columns = [{"name": d.name, "type": str(d.type_code)} for d in (cur.description or [])]
        truncated = len(rows) > limit
        rows = rows[:limit]
        return {
            "columns": [c["name"] for c in columns],
            "rows": [{k: _jsonable(v) for k, v in r.items()} for r in rows],
            "row_count": len(rows),
            "truncated": truncated,
            "result_id": self.sign_handle(sql, offset + limit) if truncated else None,
            "query_hash": hashlib.sha256(sql.encode()).hexdigest()[:12],
        }


def create_server(settings: Settings | None = None):
    settings = settings or get_settings()
    mcp, verifier = build_server(
        name="mara-warehouse", resource=settings.warehouse_resource, instructions=INSTRUCTIONS, settings=settings
    )
    wh = Warehouse(settings)
    read_only = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)

    @mcp.tool(annotations=read_only, meta={"com.mara/scope": "sql:read"})
    async def list_tables(ctx: Context) -> dict[str, Any]:
        """List the analytics tables you can query, with a one-line description of each.
        Use this first, then describe_table on the ones you need."""
        require_scope(SERVER, "list_tables")
        with tool_metrics(SERVER, "list_tables"):
            await apply_fault(ctx, settings)
            views = await wh.views()
            return {"tables": [{"name": n, "description": v["description"]} for n, v in views.items()]}

    @mcp.tool(annotations=read_only, meta={"com.mara/scope": "sql:read"})
    async def describe_table(
        ctx: Context,
        table: Annotated[str, Field(description="Table name from list_tables, e.g. 'customers'")],
    ) -> dict[str, Any]:
        """Show a table's columns, their types and plain-English descriptions, including join and
        counting hints. Read these before writing SQL."""
        require_scope(SERVER, "describe_table")
        with tool_metrics(SERVER, "describe_table"):
            await apply_fault(ctx, settings)
            views = await wh.views()
            name = table.lower().removeprefix("analytics.")
            if name not in views:
                raise ToolError(f"unknown table {table}; call list_tables")
            async with wh.pool.connection() as conn:
                cur = await conn.execute(
                    """
                    select a.attname as column, format_type(a.atttypid, a.atttypmod) as type,
                           col_description(a.attrelid, a.attnum) as description
                    from pg_attribute a join pg_class c on c.oid = a.attrelid
                    join pg_namespace n on n.oid = c.relnamespace
                    where n.nspname = 'analytics' and c.relname = %s and a.attnum > 0 and not a.attisdropped
                    order by a.attnum
                    """,
                    (name,),
                )
                cols = await cur.fetchall()
            return {
                "table": f"analytics.{name}",
                "description": views[name]["description"],
                "columns": [
                    {"name": c["column"], "type": c["type"], "description": c["description"] or ""} for c in cols
                ],
            }

    @mcp.tool(annotations=read_only, meta={"com.mara/scope": "sql:read"})
    async def run_sql(
        ctx: Context,
        sql: Annotated[str, Field(description="One SELECT over analytics tables. Max 4,000 characters.")],
        max_rows: Annotated[int, Field(ge=1, le=200, description="Rows to return (1-200)")] = 100,
        result_id: Annotated[str | None, Field(description="Pass a previous result_id to page through results")] = None,
    ) -> dict[str, Any]:
        """Run one read-only SELECT and return columns and rows. Use COUNT(DISTINCT ...) after joins to
        avoid double counting. Returns result_id when more rows exist."""
        principal = require_scope(SERVER, "run_sql")
        with tool_metrics(SERVER, "run_sql"):
            try:
                await apply_fault(ctx, settings)
            except EmptyResult:
                return {"columns": [], "rows": [], "row_count": 0, "truncated": False, "result_id": None}
            offset = 0
            if result_id:
                sql, offset = wh.open_handle(result_id)
            try:
                safe_sql = check_sql(sql, set(await wh.views()))
            except SQLRejected as exc:
                raise ToolError(f"rejected: {exc}") from exc
            regions = ["all"] if "all" in principal.regions() else principal.regions()
            try:
                result = await wh.query(safe_sql, regions, limit=max_rows, offset=offset)
            except Exception as exc:  # database errors go back to the model so it can fix the query
                message = str(exc).splitlines()[0][:300]
                raise ToolError(f"database error: {message}") from exc
            log.info("sql_ran", rows=result["row_count"], query_hash=result["query_hash"], regions=regions)
            return result

    return mcp, verifier


def create_app(settings: Settings | None = None):
    settings = settings or get_settings()
    mcp, verifier = create_server(settings)
    return asgi_app(mcp, server=SERVER, verifier=verifier, resource=settings.warehouse_resource)
