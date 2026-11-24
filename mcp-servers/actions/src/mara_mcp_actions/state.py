"""Idempotency keys and used approval ids, kept in a small SQLite file.

The actions server sits on the isolated exec network and deliberately has no
access to the platform's database, so it keeps its own tiny state.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from mara_core.paths import resolve


class ActionState:
    def __init__(self, path: str):
        self.path = resolve(path) if path != ":memory:" else Path(":memory:")
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(self.path), check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._db.executescript(
                """
                create table if not exists idempotency (key text primary key, tool text, result text, created_at text default current_timestamp);
                create table if not exists used_approvals (jti text primary key, action_id text, used_at text default current_timestamp);
                """
            )

    def get_result(self, key: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute("select result from idempotency where key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def consume_approval(self, jti: str, action_id: str) -> bool:
        """Record the approval id. False means it was already used."""
        with self._lock:
            try:
                self._db.execute("insert into used_approvals (jti, action_id) values (?, ?)", (jti, action_id))
                self._db.commit()
                return True
            except sqlite3.IntegrityError:
                return False

    def save_result(self, key: str, tool: str, result: dict[str, Any]) -> None:
        with self._lock:
            self._db.execute(
                "insert or replace into idempotency (key, tool, result) values (?, ?, ?)",
                (key, tool, json.dumps(result)),
            )
            self._db.commit()
