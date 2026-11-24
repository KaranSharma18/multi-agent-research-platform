"""The A2A AgentExecutor: turns one A2A task into one research run.

State mapping:
  working          while the engine runs (with a short progress message)
  completed        with one artifact: a data part (the brief JSON), a text part
                   (the summary) and metadata carrying `total_cost_usd`
  failed           with the reason (budget or turn cap, provider error)
  input_required   when the engine needs a clarification
  canceled         when the client cancels (the engine task is cancelled)
"""

from __future__ import annotations

import asyncio

from a2a.helpers import (
    get_data_parts,
    get_message_text,
    new_data_part,
    new_task_from_user_message,
    new_text_message,
    new_text_part,
)
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import TaskState
from mara_core.config import research_domains
from mara_core.logging import get_logger

from mara_research_agent.brief import ResearchRequest

log = get_logger(__name__)


class ResearchExecutor(AgentExecutor):
    def __init__(self, engine):
        self.engine = engine
        self._running: dict[str, asyncio.Task] = {}

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        task = context.current_task
        if task is None:
            task = new_task_from_user_message(context.message)
            await event_queue.enqueue_event(task)
        updater = TaskUpdater(event_queue=event_queue, task_id=task.id, context_id=task.context_id)

        data = next(iter(get_data_parts(list(context.message.parts))), {}) or {}
        policy = research_domains()
        request = ResearchRequest(
            goal=str(data.get("goal") or get_message_text(context.message) or "").strip(),
            max_sources=int(data.get("max_sources", policy.max_sources)),
            recency_days=int(data.get("recency_days", policy.recency_days)),
            stub=data.get("stub"),
        )
        if not request.goal:
            await updater.update_status(TaskState.TASK_STATE_REJECTED, message=new_text_message("empty research goal"))
            return

        await updater.update_status(
            TaskState.TASK_STATE_WORKING, message=new_text_message(f"Researching with the {self.engine.name} engine")
        )
        run = asyncio.create_task(self.engine.run(request))
        self._running[task.id] = run
        try:
            result = await run
        except asyncio.CancelledError:
            await updater.update_status(
                TaskState.TASK_STATE_CANCELED, message=new_text_message("cancelled by the client")
            )
            return
        except Exception as exc:
            log.exception("research_failed")
            await updater.update_status(
                TaskState.TASK_STATE_FAILED, message=new_text_message(f"research failed: {exc}"[:500])
            )
            return
        finally:
            self._running.pop(task.id, None)

        if result.status == "input_required":
            await updater.update_status(
                TaskState.TASK_STATE_INPUT_REQUIRED, message=new_text_message(result.question or "need input")
            )
            return
        if result.status != "completed" or result.brief is None:
            await updater.update_status(
                TaskState.TASK_STATE_FAILED, message=new_text_message(result.error or "no brief")
            )
            return
        brief = result.brief.model_dump()
        await updater.add_artifact(
            parts=[
                new_data_part(brief, media_type="application/json"),
                new_text_part(result.brief.summary, media_type="text/markdown"),
            ],
            name="research_brief",
            metadata={"total_cost_usd": result.cost_usd, "engine": result.engine, **result.details},
        )
        await updater.update_status(TaskState.TASK_STATE_COMPLETED, message=new_text_message("brief ready"))
        log.info(
            "research_completed", engine=result.engine, sources=len(result.brief.sources), cost_usd=result.cost_usd
        )

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        run = self._running.get(context.task_id or "")
        if run is not None:
            run.cancel()
        updater = TaskUpdater(event_queue=event_queue, task_id=context.task_id, context_id=context.context_id)
        await updater.update_status(TaskState.TASK_STATE_CANCELED, message=new_text_message("cancelled"))
