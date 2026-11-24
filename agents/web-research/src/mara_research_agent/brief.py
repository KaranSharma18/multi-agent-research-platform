"""The brief every engine returns, and the request it receives."""

from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import BaseModel, Field


class Claim(BaseModel):
    text: str
    source_urls: list[str] = Field(default_factory=list)


class Source(BaseModel):
    url: str
    title: str = ""
    published_at: str | None = None


class Brief(BaseModel):
    summary: str = Field(description="At most 300 words, plain markdown")
    claims: list[Claim] = Field(default_factory=list)
    sources: list[Source] = Field(default_factory=list)


@dataclass
class ResearchRequest:
    goal: str
    max_sources: int = 6
    recency_days: int = 30
    stub: str | None = None  # eval hook: which scripted behaviour the stub engine plays


@dataclass
class EngineResult:
    status: str  # completed | failed | input_required
    brief: Brief | None = None
    cost_usd: float = 0.0
    engine: str = ""
    error: str | None = None
    question: str | None = None
    details: dict = field(default_factory=dict)
