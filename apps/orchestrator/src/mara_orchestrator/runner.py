"""Run lifecycle: start, pause for approval, resume, cancel, recover.

Runs execute as background asyncio tasks so the API answers at once with a
run id and the client follows progress over SSE.

Durability, in plain terms:
  - The graph is checkpointed to Postgres after every step (LangGraph's
    AsyncPostgresSaver), keyed by run id.
  - A run waiting for approval costs nothing: the graph has stopped, and the
    checkpoint holds everything needed to continue. Resuming uses
    durability="sync" so the steps after approval are on disk before we move on.
  - The user's token is never persisted, on purpose. So if the process dies
    mid-research, that run cannot continue (it would need fresh task tokens) and
    is marked failed on restart. Runs past approval need no user token, so they
    resume from their checkpoint.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from datetime import date
from typing import Any

from langgraph.types import Command
from mara_core.auth.verify import Principal
from mara_core.logging import get_logger
from mara_core.pii import mask_pii

from mara_orchestrator.graph.context import RunContext, Services
from mara_orchestrator.llm.cost import CostCallback
from mara_orchestrator.prompts import versions
from mara_orchestrator.tools.interceptors import FaultPlan, ReplaySource

log = get_logger(__name__)
MAX_CONCURRENT_RUNS = 8


def new_run_id() -> str:
    return "run_" + uuid.uuid4().hex[:16]


class Runner:
    def __init__(self, services: Services, graph):
        self.services = services
        self.graph = graph
        self.settings = services.settings
        self._tasks: dict[str, asyncio.Task] = {}
        self._slots = asyncio.Semaphore(MAX_CONCURRENT_RUNS)
        self._sweeper: asyncio.Task | None = None
        self._shutting_down = False

    # ------------------------------------------------------------- helpers
    def _config(self, run_id: str, budget, tags: list[str]) -> dict[str, Any]:
        return {
            "configurable": {"thread_id": run_id},
            "callbacks": [CostCallback(budget)],
            "recursion_limit": 60,
            "run_name": "mara-run",
            "tags": ["mara", *tags],
            "metadata": {"run_id": run_id, "prompt_versions": versions()},
        }

    def _budget(self, run_id: str):
        s = self.settings
        return self.services.budgets.start(
            run_id, max_usd=s.budget_max_usd, max_calls=s.budget_max_calls, max_seconds=s.budget_max_seconds
        )

    async def _fail(self, run_id: str, message: str, *, status: str = "failed") -> None:
        await self.services.repo.update_run(run_id, status=status, error=message[:500])
        await self.services.events.emit(run_id, "error" if status == "failed" else status, {"message": message[:500]})
        await self.services.events.emit(run_id, "run_finished", {"status": status})

    # --------------------------------------------------------------- start
    async def start(
        self,
        *,
        principal: Principal,
        user_token: str,
        question: str,
        as_of: date | None = None,
        allow_actions: bool = True,
        faults: list[dict] | None = None,
        research_stub: str | None = None,
        replay_of: str | None = None,
    ) -> str:
        run_id = new_run_id()
        masked, pii_count = mask_pii(question.strip())
        as_of = as_of or date.today()
        options = {
            "allow_actions": allow_actions,
            "pii_masked": pii_count,
            "faults": faults or [],
            "research_stub": research_stub,
            "replay_of": replay_of,
        }
        await self.services.repo.create_run(
            run_id=run_id,
            user_sub=principal.subject,
            username=principal.username,
            question=masked,
            as_of=as_of,
            options=options,
            replay_of=replay_of,
        )
        await self.services.repo.audit(
            "run_started",
            actor=principal.username,
            run_id=run_id,
            data={"replay_of": replay_of, "pii_masked": pii_count},
        )
        replay = None
        if replay_of:
            replay = ReplaySource(await self.services.repo.tool_calls(replay_of))
        state = {
            "run_id": run_id,
            "question": masked,
            "as_of": as_of.isoformat(),
            "user": {
                "subject": principal.subject,
                "username": principal.username,
                "groups": sorted(principal.groups),
                "roles": sorted(principal.roles),
            },
            "options": options,
            "tasks": {},
            "results": [],
            "evidence": [],
            "actions": {},
            "revisions": 0,
            "replans": 0,
            "handled_followups": [],
            "verify_issues": [],
        }
        budget = self._budget(run_id)
        ctx = RunContext(
            services=self.services,
            run_id=run_id,
            user_token=user_token,
            budget=budget,
            faults=FaultPlan(faults) if faults and self.settings.enable_fault_injection else None,
            replay=replay,
        )
        config = self._config(run_id, budget, ["replay"] if replay_of else [])
        self._spawn(run_id, self._invoke(run_id, state, ctx, config, durability="async"))
        return run_id

    def _spawn(self, run_id: str, coro) -> None:
        task = asyncio.create_task(coro, name=f"run:{run_id}")
        self._tasks[run_id] = task
        task.add_done_callback(lambda _: self._tasks.pop(run_id, None))

    async def _invoke(self, run_id: str, payload: Any, ctx: RunContext, config: dict, *, durability: str) -> None:
        async with self._slots:
            try:
                await self.services.repo.update_run(run_id, status="running")
                try:  # a server that was down at startup gets picked up here
                    await self.services.registry.ensure_fresh()
                except Exception as exc:
                    log.warning("tool_catalog_refresh_failed", error=str(exc)[:200])
                async with asyncio.timeout(self.settings.budget_max_seconds * 3 + 120):
                    out = await self.graph.ainvoke(payload, config, context=ctx, durability=durability)
                if isinstance(out, dict) and out.get("__interrupt__"):
                    await self._paused(run_id, ctx)
            except asyncio.CancelledError:
                if not self._shutting_down:  # on shutdown, leave it for recover() on the next start
                    await self._fail(run_id, "canceled by the user", status="canceled")
                raise
            except TimeoutError:
                await self._fail(run_id, "the run took too long and was stopped")
            except Exception as exc:
                log.exception("run_failed", run_id=run_id)
                await self._fail(run_id, f"the run failed: {exc}")
            finally:
                self.services.budgets.end(run_id)

    async def _paused(self, run_id: str, ctx: RunContext) -> None:
        pending = [a for a in await self.services.repo.actions_for_run(run_id) if a["status"] == "pending_approval"]
        await self.services.repo.update_run(
            run_id,
            status="awaiting_approval",
            cost_usd=ctx.budget.snapshot()["total_usd"],
            model_calls=ctx.budget.llm_calls,
        )
        await self.services.events.emit(
            run_id,
            "approval_required",
            {
                "actions": [{k: a[k] for k in ("id", "tool", "preview", "risk", "args_sha256")} for a in pending],
                "expires_in_hours": self.settings.approval_expiry_hours,
            },
        )

    # -------------------------------------------------------------- resume
    async def resume(self, run_id: str) -> None:
        """Continue a run after every pending action has been decided."""
        if run_id in self._tasks:
            return
        if await self.services.repo.pending_count(run_id):
            return
        budget = self._budget(run_id)
        ctx = RunContext(services=self.services, run_id=run_id, user_token=None, budget=budget, resumed=True)
        config = self._config(run_id, budget, ["resume"])
        await self.services.events.emit(run_id, "resumed", {})
        self._spawn(run_id, self._invoke(run_id, Command(resume={"decided": True}), ctx, config, durability="sync"))

    # -------------------------------------------------------------- cancel
    async def cancel(self, run_id: str, actor: str) -> bool:
        task = self._tasks.get(run_id)
        run = await self.services.repo.get_run(run_id)
        if run is None:
            return False
        await self.services.repo.audit("run_canceled", actor=actor, run_id=run_id, data={})
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
            return True
        if run["status"] == "awaiting_approval":
            for action in await self.services.repo.actions_for_run(run_id):
                if action["status"] == "pending_approval":
                    await self.services.repo.decide_action(
                        action["id"], approve=False, approver=actor, note="run canceled", approval_token=None
                    )
            await self._fail(run_id, "canceled by the user", status="canceled")
            return True
        return False

    # ------------------------------------------------------------ recovery
    async def recover(self) -> None:
        """After a restart: fail runs that needed the user's token, resume the rest."""
        for run in await self.services.repo.runs_with_status(("queued", "running")):
            await self._fail(run["id"], "the orchestrator restarted while this run was in progress; please ask again")
        for run in await self.services.repo.runs_with_status(("executing",)):
            budget = self._budget(run["id"])
            ctx = RunContext(services=self.services, run_id=run["id"], user_token=None, budget=budget, resumed=True)
            log.info("run_recovering", run_id=run["id"])
            self._spawn(
                run["id"],
                self._invoke(run["id"], None, ctx, self._config(run["id"], budget, ["recover"]), durability="sync"),
            )
        for run in await self.services.repo.runs_with_status(("awaiting_approval",)):
            if not await self.services.repo.pending_count(run["id"]):
                await self.resume(run["id"])

    async def start_sweeper(self, interval: float = 60.0) -> None:
        async def sweep() -> None:
            while True:
                await asyncio.sleep(interval)
                try:
                    expired = await self.services.repo.expire_actions(self.settings.approval_expiry_hours)
                    for run_id in {row["run_id"] for row in expired}:
                        await self.services.repo.audit("actions_expired", actor="orchestrator", run_id=run_id, data={})
                        await self.resume(run_id)
                except Exception as exc:
                    log.warning("sweeper_failed", error=str(exc)[:200])

        self._sweeper = asyncio.create_task(sweep(), name="approval-sweeper")

    async def shutdown(self) -> None:
        self._shutting_down = True
        if self._sweeper:
            self._sweeper.cancel()
        for task in list(self._tasks.values()):
            task.cancel()
        for task in list(self._tasks.values()):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    def is_running(self, run_id: str) -> bool:
        return run_id in self._tasks
