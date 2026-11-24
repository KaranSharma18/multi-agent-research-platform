"""Two model tiers, each with a fallback.

The mid tier plans and verifies, where mistakes cost the most. The small tier
runs workers, synthesis and proposals, which is most of the calls. Defaults
are Gemini (see .env.example); a fallback model takes over after repeated
rate-limit or server errors, so one provider hiccup does not fail a run.

`LLM_PROVIDER=offline` swaps in a deterministic heuristic model so the whole
pipeline runs with no API key: useful for CI and for checking an install.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from langchain_core.language_models import BaseChatModel
from langchain_core.runnables import Runnable
from mara_core.settings import Settings, get_settings

Tier = Literal["mid", "small"]


def _gemini(model: str, thinking_level: str | None, settings: Settings) -> BaseChatModel:
    from langchain_google_genai import ChatGoogleGenerativeAI

    kwargs: dict = {
        "model": model,
        "retries": 2,
        "request_timeout": settings.llm_timeout_seconds,
        # Gemini 3 models are tuned for their default temperature; Google advises
        # against lowering it, so we leave it unset.
        "temperature": None,
    }
    if settings.google_api_key:
        kwargs["api_key"] = settings.google_api_key.get_secret_value()
    if thinking_level:
        kwargs["thinking_level"] = thinking_level
    return ChatGoogleGenerativeAI(**kwargs)


@lru_cache(maxsize=8)
def chat_model(tier: Tier, fallback: bool = False) -> BaseChatModel | None:
    settings = get_settings()
    if settings.llm_provider == "offline":
        from mara_orchestrator.llm.offline import OfflineChatModel

        return None if fallback else OfflineChatModel(tier=tier)
    if tier == "mid":
        name = settings.model_mid_fallback if fallback else settings.model_mid
        level = settings.thinking_level_mid
    else:
        name = settings.model_small_fallback if fallback else settings.model_small
        level = settings.thinking_level_small
    if not name:
        return None
    return _gemini(name, level, settings)


def structured(tier: Tier, schema: type) -> Runnable:
    """A model that returns `schema`, falling back to the second model on errors."""
    primary = chat_model(tier).with_structured_output(schema)  # type: ignore[union-attr]
    backup = chat_model(tier, fallback=True)
    if backup is None:
        return primary
    return primary.with_fallbacks([backup.with_structured_output(schema)])


def model_name(tier: Tier) -> str:
    settings = get_settings()
    if settings.llm_provider == "offline":
        return "offline-heuristic"
    return settings.model_mid if tier == "mid" else settings.model_small
