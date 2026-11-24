"""Postgres helpers: a connection pool and a tiny migration runner.

Migrations are plain numbered SQL files applied in order and recorded in a
`schema_migrations` table. That is less than Alembic offers, but every change
is a readable SQL file and there is nothing to learn. Each service that owns
tables ships its own migrations folder and calls `migrate()` at startup.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from mara_core.logging import get_logger

log = get_logger(__name__)


def make_pool(url: str, *, min_size: int = 1, max_size: int = 10, name: str = "pool") -> AsyncConnectionPool:
    """An async pool whose connections return rows as dicts. Call `await pool.open()`."""
    return AsyncConnectionPool(
        conninfo=url,
        min_size=min_size,
        max_size=max_size,
        open=False,
        name=name,
        kwargs={"autocommit": True, "row_factory": dict_row},
    )


async def migrate(conn: AsyncConnection, folder: Path, *, namespace: str) -> list[str]:
    """Apply every `*.sql` file in `folder` not yet recorded for `namespace`."""
    await conn.execute(
        "create table if not exists schema_migrations ("
        " namespace text not null, version text not null,"
        " applied_at timestamptz not null default now(), primary key (namespace, version))"
    )
    cur = await conn.execute("select version from schema_migrations where namespace = %s", (namespace,))
    done = {row["version"] if isinstance(row, dict) else row[0] for row in await cur.fetchall()}
    applied: list[str] = []
    for path in sorted(folder.glob("*.sql")):  # noqa: ASYNC240 - a few small files, once at startup
        if path.name in done:
            continue
        sql = path.read_text(encoding="utf-8")  # noqa: ASYNC240
        async with conn.transaction():
            await conn.execute(sql)
            await conn.execute(
                "insert into schema_migrations (namespace, version) values (%s, %s)", (namespace, path.name)
            )
        applied.append(path.name)
        log.info("migration_applied", namespace=namespace, version=path.name)
    return applied


async def fetch_all(pool: AsyncConnectionPool, sql: str, params: Sequence | dict | None = None) -> list[dict]:
    async with pool.connection() as conn:
        cur = await conn.execute(sql, params)
        return list(await cur.fetchall())


async def fetch_one(pool: AsyncConnectionPool, sql: str, params: Sequence | dict | None = None) -> dict | None:
    async with pool.connection() as conn:
        cur = await conn.execute(sql, params)
        return await cur.fetchone()


async def execute(pool: AsyncConnectionPool, sql: str, params: Sequence | dict | None = None) -> None:
    async with pool.connection() as conn:
        await conn.execute(sql, params)
