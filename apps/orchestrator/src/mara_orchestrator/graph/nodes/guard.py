"""Input guard: the cheap checks that run before any model sees the question.

Personal data is masked before the run is even stored (see runner.py). Here we
check the length and score the question for prompt-injection patterns. A
flagged question still gets an answer, because refusing would be wrong for
someone who pasted a suspicious email and asked about it, but write actions
are switched off for that run: no proposal can come out of it.
"""

from __future__ import annotations

from langgraph.runtime import Runtime
from mara_core import injection

from mara_orchestrator.graph.context import RunContext
from mara_orchestrator.graph.state import RunState

MAX_QUESTION_CHARS = 4000


async def guard(state: RunState, runtime: Runtime[RunContext]) -> dict:
    ctx = runtime.context
    question = (state.get("question") or "").strip()
    options = state.get("options") or {}
    if not question:
        return {"stop_reason": "empty_question", "guard": {"flagged": False, "notes": ["The question was empty."]}}
    if len(question) > MAX_QUESTION_CHARS:
        note = f"The question is longer than {MAX_QUESTION_CHARS} characters; please shorten it."
        return {"stop_reason": "question_too_long", "guard": {"flagged": False, "notes": [note]}}

    verdict = injection.score(question)
    actions_allowed = bool(options.get("allow_actions", True)) and not verdict.flagged
    notes = []
    if verdict.flagged:
        notes.append(
            "Part of the question reads like instructions to the assistant, so write actions are turned off "
            "for this run."
        )
    if options.get("pii_masked"):
        notes.append(
            f"{options['pii_masked']} piece(s) of personal data in the question were masked before processing."
        )
    result = {
        "flagged": verdict.flagged,
        "injection_score": round(verdict.score, 3),
        "reasons": verdict.reasons,
        "actions_allowed": actions_allowed,
        "pii_masked": options.get("pii_masked", 0),
        "notes": notes,
    }
    await ctx.emit("guard", result)
    return {"guard": result, "status": "running"}


def route_after_guard(state: RunState) -> str:
    return "finalize" if state.get("stop_reason") else "plan"
