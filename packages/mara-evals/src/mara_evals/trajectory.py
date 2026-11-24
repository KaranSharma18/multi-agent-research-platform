"""Trajectory checks with agentevals: did the run call the tools the case expects?

The recorded tool calls are turned into OpenAI-style assistant messages and
compared with a reference trajectory in "superset" mode: the run may call more
tools, but it must include the expected ones. Arguments are ignored, since many
SQL strings are equally right.
"""

from __future__ import annotations

import json

from agentevals.trajectory.match import create_trajectory_match_evaluator

_superset = create_trajectory_match_evaluator(trajectory_match_mode="superset", tool_args_match_mode="ignore")


def _messages(question: str, tools: list[tuple[str, dict]]) -> list[dict]:
    return [
        {"role": "user", "content": question},
        *[
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"function": {"name": name, "arguments": json.dumps(args)}}],
            }
            for name, args in tools
        ],
    ]


def trajectory_match(case: dict, tool_calls: list[dict]) -> float | None:
    expected = case["expect"].get("tools_all") or case["expect"].get("tools_any")
    if not expected:
        return None
    actual = [(c["tool"], c.get("args") or {}) for c in tool_calls]
    if case["expect"].get("tools_any"):
        expected = [t for t in expected if t in {name for name, _ in actual}][:1] or expected[:1]
    result = _superset(
        outputs=_messages(case["question"], actual),
        reference_outputs=_messages(case["question"], [(t, {}) for t in expected]),
    )
    return float(bool(result["score"]))
