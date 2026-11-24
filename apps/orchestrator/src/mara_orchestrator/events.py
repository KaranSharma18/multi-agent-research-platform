"""Run events: written to Postgres, fanned out to SSE streams through LISTEN/NOTIFY.

The database is the source of truth for events, so a client that disconnects
resumes from its Last-Event-ID, and an event written by any process (another
orchestrator replica, the executor) reaches every stream. NOTIFY carries only
"run_id:event_id"; listeners read the rows themselves.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections import defaultdict
from typing import Any

from mara_core.logging import get_logger
from psycopg import AsyncConnection

from mara_orchestrator.db.repo import Repo

log = get_logger(__name__)
CHANNEL = "mara_run_events"
# A stream closes after one of these; anything later (an executor result) is still
# stored and visible in the run detail and in a reopened stream.
CLOSING_EVENTS = {"run_finished"}


class EventBus:
    def __init__(self, repo: Repo, db_url: str):
        self.repo = repo
        self.db_url = db_url
        self._subscribers: dict[str, set[asyncio.Queue]] = defaultdict(set)
        self._task: asyncio.Task | None = None
        self._stopping = False

    async def emit(self, run_id: str, type_: str, data: dict[str, Any] | None = None) -> int:
        try:
            return await self.repo.add_event(run_id, type_, data)
        except Exception as exc:  # events are for humans watching; never fail a run over one
            log.warning("event_write_failed", run_id=run_id, type=type_, error=str(exc)[:200])
            return 0

    # ------------------------------------------------------------- fan-out
    async def start(self) -> None:
        self._task = asyncio.create_task(self._listen(), name="event-listener")

    async def stop(self) -> None:
        self._stopping = True
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task

    async def _listen(self) -> None:
        backoff = 1.0
        while not self._stopping:
            try:
                async with await AsyncConnection.connect(self.db_url, autocommit=True) as conn:
                    await conn.execute(f"LISTEN {CHANNEL}")
                    backoff = 1.0
                    log.info("event_listener_ready")
                    async for note in conn.notifies():
                        run_id, _, _ = note.payload.partition(":")
                        for queue in list(self._subscribers.get(run_id, ())):
                            queue.put_nowait(1)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("event_listener_reconnecting", error=str(exc)[:200], in_seconds=backoff)
                # Wake every stream so it re-reads from the database while we reconnect.
                for queues in self._subscribers.values():
                    for queue in queues:
                        queue.put_nowait(1)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    async def stream(self, run_id: str, after_id: int = 0, keepalive: float = 15.0):
        """Yield events for one run, starting after `after_id`, until a closing event."""
        queue: asyncio.Queue = asyncio.Queue()
        self._subscribers[run_id].add(queue)
        last = after_id
        try:
            while True:
                events = await self.repo.events_after(run_id, last)
                for event in events:
                    last = event["id"]
                    yield event
                    if event["type"] in CLOSING_EVENTS:
                        return
                try:
                    await asyncio.wait_for(queue.get(), timeout=keepalive)
                    while not queue.empty():
                        queue.get_nowait()
                except TimeoutError:
                    yield None  # caller sends a keep-alive comment
        finally:
            self._subscribers[run_id].discard(queue)
            if not self._subscribers[run_id]:
                self._subscribers.pop(run_id, None)
