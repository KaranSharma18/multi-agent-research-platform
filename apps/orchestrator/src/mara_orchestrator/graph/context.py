"""What nodes get besides the state: long-lived services and per-run context.

LangGraph checkpoints the state, never the context. That is deliberate: the
user's token and the budget object live here, so they are never written to
the checkpoint tables.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from mara_core.settings import Settings

from mara_orchestrator.llm.cost import BudgetRegistry, RunBudget
from mara_orchestrator.tools.interceptors import FaultPlan, ReplaySource


@dataclass
class Services:
    settings: Settings
    repo: Any  # db.repo.Repo
    events: Any  # events.EventBus
    broker: Any  # auth.broker.TokenBroker
    registry: Any  # tools.registry.ToolRegistry
    research: Any  # a2a.client.ResearchClient
    budgets: BudgetRegistry = field(default_factory=BudgetRegistry)


@dataclass
class RunContext:
    services: Services
    run_id: str
    user_token: str | None
    budget: RunBudget
    faults: FaultPlan | None = None
    replay: ReplaySource | None = None
    resumed: bool = False  # True after an approval pause or a restart: the budget started fresh

    @property
    def settings(self) -> Settings:
        return self.services.settings

    async def emit(self, type_: str, data: dict[str, Any] | None = None) -> None:
        await self.services.events.emit(self.run_id, type_, data)
