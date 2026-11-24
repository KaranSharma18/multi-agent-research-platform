"""Per-run cost and call accounting, and the hard budget.

A callback on every model call reads the provider's token usage, prices it
from config/prices.yaml and adds it to the run's budget. Nodes check the
budget before they call a model; workers check it in middleware. A run that
crosses $0.10, 24 model calls or 120 seconds stops and answers with what it
has, rather than looping on a failing task.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.outputs import LLMResult
from mara_core.config import price_table
from mara_core.logging import get_logger

log = get_logger(__name__)


class BudgetExceeded(RuntimeError):
    def __init__(self, limit: str):
        super().__init__(f"run budget exceeded: {limit}")
        self.limit = limit


@dataclass
class RunBudget:
    run_id: str
    max_usd: float
    max_calls: int
    max_seconds: float
    usd: float = 0.0
    llm_calls: int = 0
    input_tokens: int = 0
    cached_tokens: int = 0
    output_tokens: int = 0
    by_model: dict[str, dict[str, float]] = field(default_factory=dict)
    started: float = field(default_factory=time.monotonic)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add(self, model: str, input_tokens: int, output_tokens: int, cached: int = 0, extra_usd: float = 0.0) -> float:
        cost = price_table().cost(model, input_tokens, output_tokens, cached) + extra_usd
        with self._lock:
            self.usd += cost
            self.llm_calls += 1
            self.input_tokens += input_tokens
            self.output_tokens += output_tokens
            self.cached_tokens += cached
            m = self.by_model.setdefault(model, {"calls": 0, "usd": 0.0, "input": 0, "output": 0})
            m["calls"] += 1
            m["usd"] += cost
            m["input"] += input_tokens
            m["output"] += output_tokens
        return cost

    def add_external(self, usd: float, label: str) -> None:
        """Costs outside the model calls, e.g. the remote research agent's own spend."""
        with self._lock:
            m = self.by_model.setdefault(label, {"calls": 0, "usd": 0.0, "input": 0, "output": 0})
            m["calls"] += 1
            m["usd"] += usd

    def elapsed(self) -> float:
        return time.monotonic() - self.started

    def exceeded(self) -> str | None:
        if self.usd >= self.max_usd:
            return "usd"
        if self.llm_calls >= self.max_calls:
            return "calls"
        if self.elapsed() >= self.max_seconds:
            return "seconds"
        return None

    def check(self) -> None:
        limit = self.exceeded()
        if limit:
            raise BudgetExceeded(limit)

    def snapshot(self) -> dict[str, Any]:
        external = sum(v["usd"] for k, v in self.by_model.items() if k.startswith("external:"))
        return {
            "usd": round(self.usd, 6),
            "external_usd": round(external, 6),
            "total_usd": round(self.usd + external, 6),
            "llm_calls": self.llm_calls,
            "input_tokens": self.input_tokens,
            "cached_tokens": self.cached_tokens,
            "output_tokens": self.output_tokens,
            "seconds": round(self.elapsed(), 2),
            "by_model": {
                k: {kk: round(vv, 6) if isinstance(vv, float) else vv for kk, vv in v.items()}
                for k, v in self.by_model.items()
            },
        }


class BudgetRegistry:
    """Process-local budgets, one per active run."""

    def __init__(self) -> None:
        self._budgets: dict[str, RunBudget] = {}

    def start(self, run_id: str, *, max_usd: float, max_calls: int, max_seconds: float) -> RunBudget:
        budget = RunBudget(run_id, max_usd, max_calls, max_seconds)
        self._budgets[run_id] = budget
        return budget

    def get(self, run_id: str) -> RunBudget | None:
        return self._budgets.get(run_id)

    def end(self, run_id: str) -> RunBudget | None:
        return self._budgets.pop(run_id, None)


class CostCallback(AsyncCallbackHandler):
    """Adds each model call's usage to the run's budget."""

    def __init__(self, budget: RunBudget):
        self.budget = budget

    async def on_llm_end(self, response: LLMResult, **kwargs: Any) -> None:
        for generations in response.generations:
            for gen in generations:
                message = getattr(gen, "message", None)
                usage = getattr(message, "usage_metadata", None) or {}
                meta = getattr(message, "response_metadata", None) or {}
                model = meta.get("model_name") or (response.llm_output or {}).get("model_name") or "unknown"
                cached = int((usage.get("input_token_details") or {}).get("cache_read", 0) or 0)
                self.budget.add(
                    model, int(usage.get("input_tokens", 0) or 0), int(usage.get("output_tokens", 0) or 0), cached
                )
                return
