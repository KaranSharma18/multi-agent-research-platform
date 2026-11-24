"""Planning and scheduling.

The planner (mid-tier model) returns a typed TaskPlan. Code then cleans it up:
renumbers ids, drops dependencies on tasks that do not exist, breaks cycles
and caps the number of tasks. The model proposes; code decides.

`schedule` runs between waves. It marks tasks whose dependencies failed as
skipped, picks the next wave of ready tasks (at most MAX_PARALLEL_WORKERS),
and the router fans them out with LangGraph's `Send`. When nothing is left to
run it either replans (a worker asked for a follow-up and replans remain) or
moves on to synthesis.
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.runtime import Runtime
from langgraph.types import Send
from mara_core.logging import get_logger
from mara_core.settings import get_settings

from mara_orchestrator.graph.context import RunContext
from mara_orchestrator.graph.state import PlannedTask, RunState, TaskPlan
from mara_orchestrator.llm.cost import BudgetExceeded
from mara_orchestrator.llm.models import structured
from mara_orchestrator.prompts import render

log = get_logger(__name__)
MAX_TASKS = 8


def _order(task_id: str) -> int:
    return int(task_id[1:]) if task_id[1:].isdigit() else 10_000


def clean_plan(planned: list[PlannedTask], existing: dict[str, dict]) -> dict[str, dict]:
    """Turn model-written tasks into safe, numbered task records."""
    room = max(MAX_TASKS - len(existing), 0)
    planned = planned[:room]
    id_map = {t.id: f"t{len(existing) + i + 1}" for i, t in enumerate(planned)}
    tasks: dict[str, dict] = {}
    for t in planned:
        new_id = id_map[t.id]
        deps = []
        for dep in t.depends_on:
            target = id_map.get(dep) or (dep if dep in existing else None)
            if target and target != new_id and target not in deps:
                deps.append(target)
        tasks[new_id] = {
            "id": new_id,
            "kind": t.kind,
            "goal": t.goal.strip()[:500],
            "inputs": {i.name.strip()[:40]: i.value.strip()[:300] for i in t.inputs[:12] if i.name.strip()},
            "depends_on": deps,
            "tool_hints": t.tool_hints[:4],
            "status": "pending",
        }
    # Break cycles: keep removing dependencies inside a cycle until the graph is acyclic.
    while True:
        remaining = {tid: set(d for d in t["depends_on"] if d in tasks) for tid, t in tasks.items()}
        done: set[str] = set(existing)
        progressed = True
        while progressed:
            progressed = False
            for tid, deps in list(remaining.items()):
                if deps <= done:
                    done.add(tid)
                    remaining.pop(tid)
                    progressed = True
        if not remaining:
            return tasks
        victim = max(remaining, key=_order)
        log.warning("plan_cycle_broken", task=victim, deps=tasks[victim]["depends_on"])
        tasks[victim]["depends_on"] = [d for d in tasks[victim]["depends_on"] if d not in remaining]


async def plan(state: RunState, runtime: Runtime[RunContext]) -> dict:
    ctx = runtime.context
    existing = state.get("tasks") or {}
    results = state.get("results") or []
    handled = list(state.get("handled_followups") or [])
    followups = [r for r in results if r.get("followup_needed") and r["task_id"] not in handled]
    replanning = bool(existing)

    if ctx.budget.exceeded():
        return {"stop_reason": "budget"}
    system, version = render("planner", as_of=state["as_of"], toolsets=ctx.services.registry.toolset_summaries())
    human = f"User request: {state['question']}"
    if replanning:
        done = "\n".join(f"- {r['task_id']} ({r['kind']}): {r['summary'][:300]}" for r in results)
        asks = "\n".join(f"- after {r['task_id']}: {r['followup_needed']}" for r in followups)
        human += (
            f"\n\nThese tasks already ran; do not repeat them:\n{done}\n\nFollow-ups the workers asked for:\n{asks}\n\n"
            "Plan only the additional tasks needed, or return no tasks if none are needed."
        )
    try:
        result: TaskPlan = await structured("mid", TaskPlan).ainvoke([SystemMessage(system), HumanMessage(human)])
    except BudgetExceeded:
        return {"stop_reason": "budget"}
    except Exception as exc:
        log.exception("planner_failed")
        if replanning:  # keep what we have and move on
            return {
                "handled_followups": handled + [r["task_id"] for r in followups],
                "replans": state.get("replans", 0) + 1,
            }
        await ctx.emit("error", {"stage": "plan", "message": "The planner failed; try again in a moment."})
        return {"stop_reason": "planner_error", "error": str(exc)[:300]}

    if not replanning and not result.tasks:
        if result.clarifying_question:
            return {
                "stop_reason": "clarify",
                "plan": {
                    "clarifying_question": result.clarifying_question,
                    "assumptions": result.assumptions,
                    "version": version,
                },
            }
        return {"stop_reason": "no_tasks", "plan": {"assumptions": result.assumptions, "version": version}}

    new_tasks = clean_plan(result.tasks, existing)
    previous = state.get("plan") or {}
    merged_plan = {
        "answer_shape": previous.get("answer_shape") or result.answer_shape,
        "needs_action": bool(previous.get("needs_action")) or result.needs_action,
        "assumptions": list(dict.fromkeys([*(previous.get("assumptions") or []), *result.assumptions])),
        "version": version,
    }
    await ctx.emit(
        "plan",
        {
            "replan": replanning,
            "tasks": [{k: t[k] for k in ("id", "kind", "goal", "inputs", "depends_on")} for t in new_tasks.values()],
            "assumptions": merged_plan["assumptions"],
            "needs_action": merged_plan["needs_action"],
        },
    )
    update: dict[str, Any] = {"plan": merged_plan, "tasks": new_tasks}
    if replanning:
        update["handled_followups"] = handled + [r["task_id"] for r in followups]
        update["replans"] = state.get("replans", 0) + 1
    return update


def route_after_plan(state: RunState) -> str:
    return "finalize" if state.get("stop_reason") else "schedule"


async def schedule(state: RunState, runtime: Runtime[RunContext]) -> dict:
    ctx = runtime.context
    tasks = state.get("tasks") or {}
    status = {tid: t["status"] for tid, t in tasks.items()}
    updates: dict[str, dict] = {}

    for tid, st in status.items():
        if st == "running":  # a worker must always report; treat silence as failure
            status[tid] = "failed"
            updates[tid] = {"status": "failed", "error": "the worker did not report back"}

    stop = state.get("stop_reason") or ctx.budget.exceeded()
    changed = True
    while changed:
        changed = False
        for tid, t in tasks.items():
            if status[tid] != "pending":
                continue
            reason = None
            if stop:
                reason = "the run's budget was used up before this task could start"
            elif any(status.get(d) in ("failed", "skipped") for d in t.get("depends_on", [])):
                reason = "a task it depends on did not finish"
            if reason:
                status[tid] = "skipped"
                updates[tid] = {"status": "skipped", "error": reason}
                changed = True

    ready = sorted(
        (
            tid
            for tid, t in tasks.items()
            if status[tid] == "pending" and all(status.get(d) == "done" for d in t.get("depends_on", []))
        ),
        key=_order,
    )
    for tid in ready[: ctx.settings.max_parallel_workers]:
        updates[tid] = {"status": "running"}
    skipped = [tid for tid, u in updates.items() if u["status"] == "skipped"]
    if skipped:
        await ctx.emit("tasks_skipped", {"task_ids": skipped})
    return {"tasks": updates}


def worker_input(state: RunState, task: dict) -> dict:
    deps = [r for r in state.get("results") or [] if r["task_id"] in task.get("depends_on", [])]
    return {
        "run_id": state["run_id"],
        "task": task,
        "question": state["question"],
        "as_of": state["as_of"],
        "user": state.get("user") or {},
        "dependencies": deps,
        "options": state.get("options") or {},
    }


def route_after_schedule(state: RunState) -> list[Send] | str:
    tasks = state.get("tasks") or {}
    running = sorted((t for t in tasks.values() if t["status"] == "running"), key=lambda t: _order(t["id"]))
    if running:
        return [Send(f"worker_{t['kind']}", worker_input(state, t)) for t in running]
    if state.get("stop_reason"):
        return "synthesize"
    handled = set(state.get("handled_followups") or [])
    followups = [r for r in state.get("results") or [] if r.get("followup_needed") and r["task_id"] not in handled]
    if followups and state.get("replans", 0) < get_settings().max_replans and len(tasks) < MAX_TASKS:
        return "plan"
    return "synthesize"
