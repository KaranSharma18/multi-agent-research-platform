"""SQL for the orchestrator's tables, in one place.

Plain SQL through psycopg rather than an ORM: the queries are few, they matter
(locking, notifications), and reading them should not require knowing an ORM.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

from mara_core import audit
from mara_core.db import execute, fetch_all, fetch_one
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

RUN_COLUMNS = (
    "id, user_sub, username, question, as_of, status, stop_reason, options, plan, final, cost_usd, model_calls, "
    "prompt_versions, replay_of, error, created_at, updated_at, finished_at"
)
ACTION_COLUMNS = (
    "id, run_id, tool, args, args_sha256, preview, risk, reason, status, policy_reason, requested_by, approver, "
    "decided_at, decision_note, idempotency_key, attempts, result, error, created_at, updated_at, executed_at"
)
TERMINAL_RUN = ("completed", "failed", "canceled", "needs_clarification", "refused")


def _jsonable(row: dict | None) -> dict | None:
    if row is None:
        return None
    out = {}
    for key, value in row.items():
        if hasattr(value, "isoformat"):
            value = value.isoformat()
        elif value.__class__.__name__ == "Decimal":
            value = float(value)
        out[key] = value
    return out


class Repo:
    def __init__(self, pool: AsyncConnectionPool):
        self.pool = pool

    # ------------------------------------------------------------------ runs
    async def create_run(
        self,
        *,
        run_id: str,
        user_sub: str,
        username: str,
        question: str,
        as_of: date,
        options: dict,
        replay_of: str | None = None,
    ) -> None:
        await execute(
            self.pool,
            "insert into runs (id, user_sub, username, question, as_of, status, options, replay_of) "
            "values (%s, %s, %s, %s, %s, 'queued', %s, %s)",
            (run_id, user_sub, username, question, as_of, Jsonb(options), replay_of),
        )

    async def update_run(self, run_id: str, **fields: Any) -> None:
        if not fields:
            return
        sets, params = [], []
        for key, value in fields.items():
            sets.append(f"{key} = %s")
            params.append(Jsonb(value) if isinstance(value, (dict, list)) else value)
        if fields.get("status") in TERMINAL_RUN:
            sets.append("finished_at = coalesce(finished_at, now())")
        sets.append("updated_at = now()")
        await execute(self.pool, f"update runs set {', '.join(sets)} where id = %s", (*params, run_id))

    async def get_run(self, run_id: str) -> dict | None:
        return _jsonable(await fetch_one(self.pool, f"select {RUN_COLUMNS} from runs where id = %s", (run_id,)))

    async def list_runs(self, *, user_sub: str | None, limit: int = 50) -> list[dict]:
        cols = "id, username, question, status, stop_reason, cost_usd, created_at, finished_at, replay_of"
        if user_sub is None:
            rows = await fetch_all(self.pool, f"select {cols} from runs order by created_at desc limit %s", (limit,))
        else:
            rows = await fetch_all(
                self.pool,
                f"select {cols} from runs where user_sub = %s order by created_at desc limit %s",
                (user_sub, limit),
            )
        return [_jsonable(r) for r in rows]

    async def runs_with_status(self, statuses: tuple[str, ...]) -> list[dict]:
        rows = await fetch_all(self.pool, f"select {RUN_COLUMNS} from runs where status = any(%s)", (list(statuses),))
        return [_jsonable(r) for r in rows]

    # ---------------------------------------------------------------- events
    async def add_event(self, run_id: str, type_: str, data: dict | None = None) -> int:
        row = await fetch_one(
            self.pool,
            "insert into run_events (run_id, type, data) values (%s, %s, %s) returning id",
            (run_id, type_, Jsonb(data or {})),
        )
        return int(row["id"])

    async def events_after(self, run_id: str, after_id: int = 0, limit: int = 500) -> list[dict]:
        rows = await fetch_all(
            self.pool,
            "select id, type, data, created_at from run_events where run_id = %s and id > %s order by id limit %s",
            (run_id, after_id, limit),
        )
        return [_jsonable(r) for r in rows]

    # ------------------------------------------------------------ tool calls
    async def record_tool_call(
        self,
        *,
        run_id: str,
        task_id: str,
        seq: int,
        server: str,
        tool: str,
        args: dict,
        args_sha256: str,
        result: Any,
        is_error: bool,
        latency_ms: int,
    ) -> None:
        await execute(
            self.pool,
            "insert into tool_calls (run_id, task_id, seq, server, tool, args, args_sha256, result, "
            "is_error, latency_ms) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (run_id, task_id, seq, server, tool, Jsonb(args), args_sha256, Jsonb(result), is_error, latency_ms),
        )

    async def tool_calls(self, run_id: str) -> list[dict]:
        rows = await fetch_all(
            self.pool,
            "select id, task_id, seq, server, tool, args, args_sha256, result, is_error, "
            "latency_ms, created_at from tool_calls where run_id = %s order by id",
            (run_id,),
        )
        return [_jsonable(r) for r in rows]

    # --------------------------------------------------------------- actions
    async def insert_action(self, action: dict) -> None:
        await execute(
            self.pool,
            "insert into actions (id, run_id, tool, args, args_sha256, preview, risk, reason, status, "
            "policy_reason, requested_by, idempotency_key) values "
            "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) on conflict (id) do nothing",
            (
                action["id"],
                action["run_id"],
                action["tool"],
                Jsonb(action["args"]),
                action["args_sha256"],
                action["preview"],
                action["risk"],
                action.get("reason"),
                action["status"],
                action.get("policy_reason"),
                action["requested_by"],
                action["idempotency_key"],
            ),
        )

    async def get_action(self, action_id: str) -> dict | None:
        return _jsonable(
            await fetch_one(self.pool, f"select {ACTION_COLUMNS} from actions where id = %s", (action_id,))
        )

    async def actions_for_run(self, run_id: str) -> list[dict]:
        rows = await fetch_all(
            self.pool, f"select {ACTION_COLUMNS} from actions where run_id = %s order by created_at, id", (run_id,)
        )
        return [_jsonable(r) for r in rows]

    async def list_actions(self, status: str | None = None, limit: int = 100) -> list[dict]:
        sql = (
            f"select a.{ACTION_COLUMNS.replace(', ', ', a.')}, r.question, r.username from actions a "
            "join runs r on r.id = a.run_id"
        )
        if status:
            rows = await fetch_all(
                self.pool, sql + " where a.status = %s order by a.created_at desc limit %s", (status, limit)
            )
        else:
            rows = await fetch_all(self.pool, sql + " order by a.created_at desc limit %s", (limit,))
        return [_jsonable(r) for r in rows]

    async def actions_today(self, requested_by: str) -> int:
        row = await fetch_one(
            self.pool,
            "select count(*) as n from actions where requested_by = %s and "
            "created_at > now() - interval '24 hours' and status <> 'rejected_by_policy'",
            (requested_by,),
        )
        return int(row["n"]) if row else 0

    async def decide_action(
        self, action_id: str, *, approve: bool, approver: str, note: str | None, approval_token: str | None
    ) -> dict | None:
        """Record a human decision. Only a pending action can be decided, and only once."""
        row = await fetch_one(
            self.pool,
            "update actions set status = %s, approver = %s, decision_note = %s, approval_token = %s, decided_at = now(), "
            "updated_at = now() where id = %s and status = 'pending_approval' returning id, run_id, status",
            ("approved" if approve else "rejected", approver, note, approval_token, action_id),
        )
        return _jsonable(row)

    async def pending_count(self, run_id: str) -> int:
        row = await fetch_one(
            self.pool, "select count(*) as n from actions where run_id = %s and status = 'pending_approval'", (run_id,)
        )
        return int(row["n"]) if row else 0

    async def expire_actions(self, hours: int) -> list[dict]:
        rows = await fetch_all(
            self.pool,
            "update actions set status = 'expired', updated_at = now() where status = "
            "'pending_approval' and created_at < now() - make_interval(hours => %s) "
            "returning id, run_id",
            (hours,),
        )
        return [dict(r) for r in rows]

    # ----------------------------------------------------------------- audit
    async def audit(self, event: str, actor: str, run_id: str | None = None, data: dict | None = None) -> None:
        await audit.append(
            self.pool, event=event, actor=actor, run_id=run_id, data=json.loads(json.dumps(data or {}, default=str))
        )
