"""Typed access to the YAML files in `config/`.

Policy lives in versioned files rather than code so a reviewer can see, in one
place, which repos an agent may write to, which scopes a task gets, and what a
token costs. Each loader validates the file with Pydantic, so a typo fails at
startup instead of in the middle of a run.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from mara_core.paths import config_dir


def _load_yaml(name: str) -> dict:
    path = config_dir() / name
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


class TaskKindPolicy(BaseModel):
    audience: str
    scopes: list[str]


class ScopePolicy(BaseModel):
    """Which audience and scopes each task kind gets, and which scope each tool needs."""

    task_kinds: dict[str, TaskKindPolicy]
    tools: dict[str, dict[str, str]]
    core_tools: dict[str, list[str]] = Field(default_factory=dict)
    catalog_scope: str = "catalog:read"

    def scope_for_tool(self, server: str, tool: str) -> str | None:
        return self.tools.get(server, {}).get(tool)

    def server_for_kind(self, kind: str) -> str:
        return self.task_kinds[kind].audience


class Limits(BaseModel):
    max_actions_per_run: int = 5
    max_actions_per_user_per_day: int = 20


class Allowlists(BaseModel):
    github_repos: list[str] = Field(default_factory=list)
    slack_channels: list[str] = Field(default_factory=list)
    email_domains: list[str] = Field(default_factory=list)
    url_domains: list[str] = Field(default_factory=list)
    email_enabled: bool = False
    limits: Limits = Field(default_factory=Limits)


class ModelPrice(BaseModel):
    input: float
    cached_input: float = 0.0
    output: float = 0.0


class PriceTable(BaseModel):
    models: dict[str, ModelPrice]
    embeddings: dict[str, ModelPrice] = Field(default_factory=dict)
    default: ModelPrice

    def for_model(self, model: str) -> ModelPrice:
        # Providers sometimes return versioned names ("models/gemini-3.8-flash-001").
        name = model.removeprefix("models/")
        if name in self.models:
            return self.models[name]
        for known, price in self.models.items():
            if name.startswith(known):
                return price
        return self.default

    def cost(self, model: str, input_tokens: int, output_tokens: int, cached_tokens: int = 0) -> float:
        price = self.for_model(model)
        fresh = max(input_tokens - cached_tokens, 0)
        return (fresh * price.input + cached_tokens * price.cached_input + output_tokens * price.output) / 1_000_000


class ResearchDomains(BaseModel):
    allow: list[str] = Field(default_factory=list)
    block: list[str] = Field(default_factory=list)
    max_sources: int = 6
    recency_days: int = 30

    def is_allowed(self, host: str) -> bool:
        host = host.lower().split(":")[0]

        def matches(pattern: str) -> bool:
            pattern = pattern.lower()
            return host == pattern or host.endswith("." + pattern)

        if any(matches(p) for p in self.block):
            return False
        return not self.allow or any(matches(p) for p in self.allow)


@lru_cache(maxsize=1)
def scope_policy() -> ScopePolicy:
    return ScopePolicy.model_validate(_load_yaml("scopes.yaml"))


@lru_cache(maxsize=1)
def allowlists() -> Allowlists:
    return Allowlists.model_validate(_load_yaml("allowlists.yaml"))


@lru_cache(maxsize=1)
def price_table() -> PriceTable:
    return PriceTable.model_validate(_load_yaml("prices.yaml"))


@lru_cache(maxsize=1)
def research_domains() -> ResearchDomains:
    return ResearchDomains.model_validate(_load_yaml("research_domains.yaml"))


def load_prompt(name: str) -> tuple[str, str]:
    """Return (text, version) for `config/prompts/<name>.md`.

    The first line of each prompt file is `<!-- version: name@N -->`. The version
    is stamped on traces and eval experiments, so a score change can be tied to
    the prompt change that caused it.
    """
    path: Path = config_dir() / "prompts" / f"{name}.md"
    text = path.read_text(encoding="utf-8")
    first, _, rest = text.partition("\n")
    version = f"{name}@0"
    if first.startswith("<!--") and "version:" in first:
        version = first.split("version:", 1)[1].strip().rstrip("->").strip()
        text = rest
    return text.strip(), version


def clear_caches() -> None:
    for fn in (scope_policy, allowlists, price_table, research_domains):
        fn.cache_clear()
