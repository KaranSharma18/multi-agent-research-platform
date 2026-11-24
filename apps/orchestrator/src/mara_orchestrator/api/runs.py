"""Runs: ask a question, follow it live, inspect it, cancel it, replay it."""

from __future__ import annotations

import json
from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from mara_orchestrator.api.deps import User, can_view_run, current_user

router = APIRouter(prefix="/v1", tags=["runs"])


class FaultRule(BaseModel):
    server: Literal["knowledge", "warehouse", "live_data"] | None = None
    tool: str | None = None
    fault: str = Field(description="timeout | error | empty | latency=<ms>")
    times: int = Field(default=1, ge=1, le=10)


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    as_of: date | None = Field(default=None, description="Treat this date as today (evals and replays use it)")
    allow_actions: bool = True
    faults: list[FaultRule] | None = Field(default=None, description="Eval only: needs ENABLE_FAULT_INJECTION=true")
    research_stub: Literal["complete", "fail", "stall", "input_required", "inject"] | None = None


class RunCreated(BaseModel):
    run_id: str
    status: str
    events_url: str


def _runner(request: Request):
    return request.app.state.runner


async def _visible_run(request: Request, run_id: str, user: User) -> dict:
    run = await request.app.state.repo.get_run(run_id)
    if run is None or not can_view_run(user, run):
        raise HTTPException(404, detail="run not found")
    return run


@router.post("/runs", status_code=202, response_model=RunCreated)
async def create_run(body: AskRequest, request: Request, user: User = Depends(current_user)) -> RunCreated:
    settings = request.app.state.settings
    if (body.faults or body.research_stub) and not settings.enable_fault_injection:
        raise HTTPException(400, detail="fault injection is disabled (set ENABLE_FAULT_INJECTION=true for evals)")
    run_id = await _runner(request).start(
        principal=user.principal,
        user_token=user.token,
        question=body.question,
        as_of=body.as_of,
        allow_actions=body.allow_actions,
        faults=[f.model_dump() for f in body.faults or []],
        research_stub=body.research_stub,
    )
    return RunCreated(run_id=run_id, status="queued", events_url=f"/v1/runs/{run_id}/events")


@router.get("/runs")
async def list_runs(request: Request, limit: int = Query(50, ge=1, le=200), user: User = Depends(current_user)):
    scope = None if user.has_role("admin") else user.principal.subject
    return {"runs": await request.app.state.repo.list_runs(user_sub=scope, limit=limit)}


@router.get("/runs/{run_id}")
async def get_run(run_id: str, request: Request, user: User = Depends(current_user)):
    run = await _visible_run(request, run_id, user)
    run["actions"] = await request.app.state.repo.actions_for_run(run_id)
    run["live"] = _runner(request).is_running(run_id)
    return run


@router.get("/runs/{run_id}/events")
async def run_events(run_id: str, request: Request, after: int = Query(0, ge=0), user: User = Depends(current_user)):
    await _visible_run(request, run_id, user)
    last = request.headers.get("last-event-id")
    start = int(last) if last and last.isdigit() else after
    bus = request.app.state.events

    async def gen():
        async for event in bus.stream(run_id, start):
            if await request.is_disconnected():
                return
            if event is None:
                continue  # sse-starlette sends its own pings
            yield {"id": str(event["id"]), "event": event["type"], "data": json.dumps(event["data"], default=str)}

    return EventSourceResponse(gen(), ping=15)


@router.post("/runs/{run_id}/cancel")
async def cancel_run(run_id: str, request: Request, user: User = Depends(current_user)):
    run = await _visible_run(request, run_id, user)
    if run["user_sub"] != user.principal.subject and not user.has_role("admin"):
        raise HTTPException(403, detail="only the person who asked (or an admin) can cancel a run")
    if not await _runner(request).cancel(run_id, actor=user.name):
        raise HTTPException(409, detail=f"run is {run['status']} and cannot be canceled")
    return {"run_id": run_id, "status": "canceled"}


@router.get("/runs/{run_id}/tool-calls")
async def tool_calls(run_id: str, request: Request, user: User = Depends(current_user)):
    await _visible_run(request, run_id, user)
    return {"tool_calls": await request.app.state.repo.tool_calls(run_id)}


@router.post("/runs/{run_id}/replay", status_code=202, response_model=RunCreated)
async def replay_run(run_id: str, request: Request, user: User = Depends(current_user)) -> RunCreated:
    """Run the same question again against the recorded tool results. No tool calls leave the process."""
    run = await _visible_run(request, run_id, user)
    if run["status"] not in ("completed", "failed", "needs_clarification"):
        raise HTTPException(409, detail="only finished runs can be replayed")
    options = run.get("options") or {}
    new_id = await _runner(request).start(
        principal=user.principal,
        user_token=user.token,
        question=run["question"],
        as_of=date.fromisoformat(run["as_of"]),
        allow_actions=False,
        replay_of=run_id,
        research_stub=options.get("research_stub"),
    )
    return RunCreated(run_id=new_id, status="queued", events_url=f"/v1/runs/{new_id}/events")


@router.get("/me")
async def me(user: User = Depends(current_user)):
    p = user.principal
    return {
        "subject": p.subject,
        "username": p.username,
        "groups": sorted(p.groups),
        "roles": sorted(p.roles),
        "name": p.claims.get("name"),
    }


@router.get("/tools")
async def tools(request: Request, user: User = Depends(current_user)):
    registry = request.app.state.registry
    return {
        "servers": {
            server: [
                {
                    "name": c.name,
                    "scope": c.scope,
                    "enabled": c.enabled,
                    "pin": c.pin_hash[:12],
                    "description": c.description.splitlines()[0][:160] if c.description else "",
                }
                for c in cards.values()
            ]
            for server, cards in registry.cards.items()
        }
    }
