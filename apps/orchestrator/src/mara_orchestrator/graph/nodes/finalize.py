"""Finalize: one place that decides the run's outcome and writes it down."""

from __future__ import annotations

import re

from langgraph.runtime import Runtime

from mara_orchestrator.graph.context import RunContext
from mara_orchestrator.graph.state import RunState
from mara_orchestrator.prompts import versions

CITE_RE = re.compile(r"ev_[A-Za-z0-9]+_\d+")
MESSAGES = {
    "empty_question": "Please type a question.",
    "question_too_long": "That question is too long. Please shorten it to under 4,000 characters.",
    "planner_error": "I could not plan this request because the model call failed. Please try again in a moment.",
    "no_tasks": "I could not turn this into something the platform's data sources can answer.",
}


def combine_costs(before: dict | None, after: dict) -> dict:
    """Spend before an approval pause plus spend after it."""
    if not before:
        return after
    out = dict(after)
    for key in ("usd", "external_usd", "total_usd", "llm_calls", "input_tokens", "cached_tokens", "output_tokens"):
        out[key] = round(before.get(key, 0) + after.get(key, 0), 6)
    out["seconds"] = round(before.get("seconds", 0) + after.get("seconds", 0), 2)
    out["by_model"] = {
        **before.get("by_model", {}),
        **{k: v for k, v in after.get("by_model", {}).items() if k not in before.get("by_model", {})},
    }
    return out


def citations(answer: str, evidence: list[dict]) -> list[dict]:
    by_id = {e["id"]: e for e in evidence}
    out = []
    for ev_id in dict.fromkeys(CITE_RE.findall(answer)):
        e = by_id.get(ev_id)
        if e:
            out.append(
                {
                    "id": ev_id,
                    "title": e["title"],
                    "uri": e["uri"],
                    "source": e["source"],
                    "created_at": e.get("created_at"),
                    "snippet": e["content"][:300],
                    "flagged": e["flagged"],
                }
            )
    return out


async def finalize(state: RunState, runtime: Runtime[RunContext]) -> dict:
    ctx = runtime.context
    stop = state.get("stop_reason")
    plan = state.get("plan") or {}
    verdict = state.get("verdict") or {}
    guard = state.get("guard") or {}
    actions = list((state.get("actions") or {}).values())
    evidence = state.get("evidence") or []

    if stop in ("empty_question", "question_too_long"):
        status, answer = "refused", MESSAGES[stop]
    elif stop == "clarify":
        status, answer = "needs_clarification", plan.get("clarifying_question") or "Could you clarify the request?"
    elif stop in ("planner_error", "no_tasks"):
        status, answer = ("failed" if stop == "planner_error" else "completed"), MESSAGES[stop]
    else:
        status, answer = "completed", state.get("answer") or "No answer was produced."

    notes = list(guard.get("notes") or [])
    if verdict and not verdict.get("passed") and status == "completed":
        notes.append(
            "The automatic check could not confirm every part of this answer: "
            + "; ".join(verdict.get("issues", [])[:3])
        )
        if plan.get("needs_action"):
            notes.append("No actions were proposed because the answer did not pass verification.")
    if stop == "budget":
        notes.append("The run stopped early because it reached its cost, call or time budget.")
    if plan.get("needs_action") and not guard.get("actions_allowed", True):
        notes.append("Write actions were disabled for this run.")

    budget = combine_costs(state.get("cost_checkpoint") if ctx.resumed else None, ctx.budget.snapshot())
    final = {
        "status": status,
        "answer": answer,
        "citations": citations(answer, evidence),
        "assumptions": plan.get("assumptions") or [],
        "notes": notes,
        "verification": verdict or None,
        "actions": [
            {
                k: a.get(k)
                for k in ("id", "tool", "preview", "risk", "status", "policy_reason", "approver", "result", "error")
            }
            for a in actions
        ],
        "tasks": [
            {"id": t["id"], "kind": t["kind"], "goal": t["goal"], "status": t["status"], "error": t.get("error")}
            for t in (state.get("tasks") or {}).values()
        ],
        "stop_reason": stop,
        "cost": budget,
        "prompt_versions": versions(),
    }
    await ctx.services.repo.update_run(
        state["run_id"],
        status=status,
        stop_reason=stop,
        final=final,
        plan=plan or None,
        cost_usd=budget.get("total_usd", 0.0),
        model_calls=budget.get("llm_calls", 0),
        prompt_versions=final["prompt_versions"],
        error=state.get("error"),
    )
    await ctx.emit("final", final)
    await ctx.emit("run_finished", {"status": status})
    return {"final": final, "status": status}
