"""A hash-chained audit log.

Each entry stores the hash of the previous entry, so deleting or editing a row
breaks the chain and `verify_chain` reports where. It does not stop someone
with database superuser rights from rewriting everything, but it makes quiet,
partial tampering visible, which is the realistic threat for a demo-sized
system. Writes are serialised with an advisory lock so concurrent writers
cannot fork the chain.
"""

from __future__ import annotations

from typing import Any

from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from mara_core.hashing import canonical_json, sha256_hex

_LOCK_KEY = 7_202_611  # any constant; identifies the audit chain's advisory lock

GENESIS = "0" * 64


async def append(
    pool: AsyncConnectionPool, *, event: str, actor: str, run_id: str | None = None, data: dict[str, Any] | None = None
) -> str:
    data = data or {}
    async with pool.connection() as conn, conn.transaction():
        await conn.execute("select pg_advisory_xact_lock(%s)", (_LOCK_KEY,))
        cur = await conn.execute("select entry_hash from audit_log order by id desc limit 1")
        row = await cur.fetchone()
        prev = (row["entry_hash"] if isinstance(row, dict) else row[0]) if row else GENESIS
        entry_hash = sha256_hex(prev + canonical_json({"event": event, "actor": actor, "run_id": run_id, "data": data}))
        await conn.execute(
            "insert into audit_log (event, actor, run_id, data, prev_hash, entry_hash) values (%s, %s, %s, %s, %s, %s)",
            (event, actor, run_id, Jsonb(data), prev, entry_hash),
        )
    return entry_hash


async def verify_chain(pool: AsyncConnectionPool) -> tuple[bool, int | None]:
    """Return (ok, first_bad_id)."""
    prev = GENESIS
    async with pool.connection() as conn:
        cur = await conn.execute(
            "select id, event, actor, run_id, data, prev_hash, entry_hash from audit_log order by id"
        )
        async for row in cur:
            expected = sha256_hex(
                prev
                + canonical_json(
                    {"event": row["event"], "actor": row["actor"], "run_id": row["run_id"], "data": row["data"]}
                )
            )
            if row["prev_hash"] != prev or row["entry_hash"] != expected:
                return False, row["id"]
            prev = row["entry_hash"]
    return True, None
