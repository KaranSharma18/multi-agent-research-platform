"""Optional LLM judge (autoevals Factuality) running on Gemini.

Code scorers decide pass/fail; the judge adds a second opinion on whether the
answer is factually consistent with the expected facts, which catches answers
that contain the right number for the wrong reason. Gemini is reached through
its OpenAI-compatible endpoint, which autoevals supports out of the box.
"""

from __future__ import annotations

from typing import Any

from mara_core.settings import Settings

GEMINI_OPENAI_BASE = "https://generativelanguage.googleapis.com/v1beta/openai/"


def make_judge(settings: Settings):
    if not settings.google_api_key:
        raise SystemExit("--judge needs GOOGLE_API_KEY (the judge runs on Gemini)")
    from autoevals import Factuality, init
    from openai import AsyncOpenAI

    init(AsyncOpenAI(api_key=settings.google_api_key.get_secret_value(), base_url=GEMINI_OPENAI_BASE), is_async=True)
    scorer = Factuality(model=settings.judge_model)

    async def judge(case: dict, run: dict) -> float | None:
        answer = (run.get("final") or {}).get("answer") or ""
        expected = "; ".join(group[0] for group in case["expect"].get("facts") or [])
        try:
            result: Any = await scorer.eval_async(input=case["question"], output=answer, expected=expected)
            return float(result.score or 0.0)
        except Exception:
            return None

    return judge
