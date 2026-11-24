"""The approvals inbox. The only place an approval token is ever minted.

Approving checks, in order: the caller has the `approver` role; the action is
still pending; for high-risk actions, the approver is not the person who asked
(four eyes); and the stored arguments still hash to what was proposed. Only then
is a one-time token signed, binding this approver to these exact arguments.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from mara_core.actions import InvalidAction, action_hash
from pydantic import BaseModel, Field

from mara_orchestrator.api.deps import User, current_user

router = APIRouter(prefix="/v1/approvals", tags=["approvals"])


class Decision(BaseModel):
    decision: Literal["approve", "reject"]
    note: str | None = Field(default=None, max_length=500)


def _require_approver(user: User) -> None:
    if not user.has_role("approver"):
        raise HTTPException(403, detail="you need the approver role to review actions")


@router.get("")
async def list_approvals(
    request: Request, status: str | None = Query("pending_approval"), user: User = Depends(current_user)
):
    _require_approver(user)
    return {"actions": await request.app.state.repo.list_actions(status=status or None)}


@router.post("/{action_id}")
async def decide(action_id: str, body: Decision, request: Request, user: User = Depends(current_user)):
    _require_approver(user)
    repo = request.app.state.repo
    action = await repo.get_action(action_id)
    if action is None:
        raise HTTPException(404, detail="action not found")
    if action["status"] != "pending_approval":
        raise HTTPException(409, detail=f"action is already {action['status']}")
    run = await repo.get_run(action["run_id"])
    if body.decision == "approve" and action["risk"] == "high" and run and run["user_sub"] == user.principal.subject:
        raise HTTPException(403, detail="high-risk actions must be approved by someone other than the requester")

    token = None
    if body.decision == "approve":
        try:
            current = action_hash(action["tool"], action["args"])
        except InvalidAction as exc:
            raise HTTPException(409, detail=f"stored arguments are no longer valid: {exc}") from exc
        if current != action["args_sha256"]:
            raise HTTPException(409, detail="stored arguments changed after they were proposed; refusing to approve")
        token = request.app.state.approval_signer.mint(
            run_id=action["run_id"], action_id=action_id, tool=action["tool"], args_sha256=current, approver=user.name
        )
    row = await repo.decide_action(
        action_id, approve=body.decision == "approve", approver=user.name, note=body.note, approval_token=token
    )
    if row is None:
        raise HTTPException(409, detail="action was decided by someone else a moment ago")
    await repo.audit(
        f"action_{row['status']}",
        actor=user.name,
        run_id=action["run_id"],
        data={"action_id": action_id, "tool": action["tool"], "args_sha256": action["args_sha256"], "note": body.note},
    )
    await request.app.state.events.emit(
        action["run_id"], "action_decided", {"action_id": action_id, "status": row["status"], "approver": user.name}
    )
    remaining = await repo.pending_count(action["run_id"])
    if remaining == 0:
        await request.app.state.runner.resume(action["run_id"])
    return {"id": action_id, "status": row["status"], "pending_in_run": remaining}
