"""Compare an original run with its replay.

Replay re-runs the same question with every tool result served from the
recording, so any difference comes from the models or prompts, not from data
that changed underneath. That makes it the tool for "did my prompt change break
this run?" and for debugging a bad answer without touching live systems.
"""

from __future__ import annotations

import difflib
from typing import Any


async def diff_runs(client: Any, token: str, original_id: str, replay_id: str) -> dict:
    original = await client.run(token, original_id)
    replay = await client.run(token, replay_id)
    a = (original.get("final") or {}).get("answer") or ""
    b = (replay.get("final") or {}).get("answer") or ""
    calls_a = [(c["server"], c["tool"], c["args_sha256"][:12]) for c in await client.tool_calls(token, original_id)]
    events_b = await client.tool_calls(token, replay_id)  # replays record nothing; kept for symmetry
    plan_a = [(t["kind"], t["goal"]) for t in (original.get("final") or {}).get("tasks", [])]
    plan_b = [(t["kind"], t["goal"]) for t in (replay.get("final") or {}).get("tasks", [])]
    return {
        "original": original_id,
        "replay": replay_id,
        "status": [original["status"], replay["status"]],
        "answer_identical": a == b,
        "answer_similarity": round(difflib.SequenceMatcher(None, a, b).ratio(), 3),
        "answer_diff": list(difflib.unified_diff(a.splitlines(), b.splitlines(), "original", "replay", lineterm=""))[
            :60
        ],
        "plan_identical": plan_a == plan_b,
        "recorded_tool_calls": len(calls_a),
        "replay_live_tool_calls": len(events_b),
        "cost": [
            (original.get("final") or {}).get("cost", {}).get("total_usd"),
            (replay.get("final") or {}).get("cost", {}).get("total_usd"),
        ],
        "prompt_versions": [
            (original.get("final") or {}).get("prompt_versions"),
            (replay.get("final") or {}).get("prompt_versions"),
        ],
    }
