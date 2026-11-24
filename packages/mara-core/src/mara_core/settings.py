"""All runtime settings, read from environment variables and an optional `.env`.

One settings class for the whole platform keeps names consistent between
services and between `docker-compose.yml`, `.env.example` and the docs. Each
service reads only the fields it needs.

Resource identifiers (`*_RESOURCE`) are the canonical names that go into token
audiences. They are identifiers, not addresses: a service reached through
`localhost` during development still checks tokens against its in-cluster name.
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Literal

from dotenv import dotenv_values
from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from mara_core.paths import repo_root

ENV_FILE = repo_root() / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        env_ignore_empty=True,  # GOOGLE_API_KEY= (blank) means "not set", not an empty key
        extra="ignore",
        case_sensitive=False,
    )

    # --- General -----------------------------------------------------------
    environment: Literal["dev", "test", "prod"] = "dev"
    log_level: str = "INFO"
    log_json: bool = False

    # --- Models (Gemini by default) ---------------------------------------
    llm_provider: Literal["gemini", "offline"] = "gemini"
    google_api_key: SecretStr | None = Field(
        default=None, validation_alias=AliasChoices("GOOGLE_API_KEY", "GEMINI_API_KEY")
    )
    model_mid: str = "gemini-3.8-flash"
    model_small: str = "gemini-3.5-flash-lite"
    model_mid_fallback: str | None = "gemini-3.6-flash"
    model_small_fallback: str | None = "gemini-3.1-flash-lite"
    thinking_level_mid: str | None = "low"
    thinking_level_small: str | None = "minimal"
    llm_timeout_seconds: float = 30.0
    judge_model: str = "gemini-3.6-flash"

    embedding_provider: Literal["gemini", "hash"] = "gemini"
    embedding_model: str = "gemini-embedding-001"
    embedding_dim: int = 768
    reranker: Literal["none", "llm"] = "none"

    # --- Stores ------------------------------------------------------------
    app_db_url: str = "postgresql://mara:mara@localhost:5432/mara"
    warehouse_ro_url: str = "postgresql://agent_ro:agent_ro@localhost:5432/warehouse"
    warehouse_admin_url: str = "postgresql://warehouse:warehouse@localhost:5432/warehouse"
    qdrant_url: str = "http://localhost:6333"
    qdrant_collection: str = "kb_chunks"

    # --- Identity ------------------------------------------------------------
    auth_issuer: str = "http://token-service:8090"
    auth_token_url: str = "http://localhost:8090/token"
    auth_jwks_url: str = "http://localhost:8090/jwks.json"
    auth_leeway_seconds: int = 30

    orchestrator_client_id: str = "mara-orchestrator"
    orchestrator_client_secret: SecretStr | None = SecretStr("dev-orchestrator-secret")
    orchestrator_client_key_file: str | None = None
    executor_client_id: str = "mara-executor"
    executor_client_secret: SecretStr | None = SecretStr("dev-executor-secret")
    executor_client_key_file: str | None = None
    ui_client_id: str = "mara-ui"

    # Canonical resource identifiers (token audiences)
    orchestrator_resource: str = "mara-orchestrator"
    knowledge_resource: str = "http://mcp-knowledge:8101/mcp"
    warehouse_resource: str = "http://mcp-warehouse:8102/mcp"
    live_data_resource: str = "http://mcp-live-data:8103/mcp"
    actions_resource: str = "http://mcp-actions:8104/mcp"
    research_agent_resource: str = "http://web-research-agent:8200"

    # Where to actually connect
    mcp_knowledge_url: str = "http://localhost:8101/mcp"
    mcp_warehouse_url: str = "http://localhost:8102/mcp"
    mcp_live_data_url: str = "http://localhost:8103/mcp"
    mcp_actions_url: str = "http://localhost:8104/mcp"
    research_agent_url: str = "http://localhost:8200"
    mock_apis_url: str = "http://localhost:8300"
    orchestrator_url: str = "http://localhost:8000"

    # --- Keys ----------------------------------------------------------------
    approval_private_key_file: str = ".secrets/approval_signing_key.pem"
    approval_public_key_file: str = ".secrets/approval_signing_key.pub.pem"
    agent_card_private_key_file: str = ".secrets/agent_card_key.pem"
    agent_card_public_key_file: str = ".secrets/agent_card_key.pub.pem"
    token_service_key_file: str = ".secrets/token_service_key.pem"
    result_handle_secret: SecretStr = SecretStr("dev-result-handle-secret")

    # --- Budgets -------------------------------------------------------------
    budget_max_usd: float = 0.10
    budget_max_calls: int = 24
    budget_max_seconds: int = 120
    max_parallel_workers: int = 4
    worker_tool_call_limit: int = 6
    max_replans: int = 2
    max_revisions: int = 1
    a2a_timeout_seconds: float = 90.0
    approval_token_ttl_seconds: int = 600
    approval_expiry_hours: int = 24

    # --- Faults and test hooks ----------------------------------------------
    enable_fault_injection: bool = False

    # --- Research agent ------------------------------------------------------
    research_engine: Literal["auto", "claude", "gemini", "stub"] = "auto"
    anthropic_api_key: SecretStr | None = None
    research_model: str = "gemini-3.8-flash"
    research_max_turns: int = 12
    research_max_budget_usd: float = 0.25

    # --- Actions -------------------------------------------------------------
    actions_backend: Literal["sandbox", "live"] = "sandbox"
    github_token: SecretStr | None = None
    slack_webhook_url: str | None = None
    smtp_host: str = "localhost"
    smtp_port: int = 1025
    actions_state_db: str = ""  # empty: <data dir>/actions_state.sqlite3

    live_data_backend: Literal["mock", "github"] = "mock"

    # --- Observability -------------------------------------------------------
    otel_exporter_otlp_endpoint: str | None = None
    langsmith_project: str | None = None
    braintrust_project: str = "mara"

    # --- Service binding -----------------------------------------------------
    host: str = "0.0.0.0"
    cors_origins: str = "http://localhost:3000,http://localhost:5173"

    def resource_for(self, key: str) -> str:
        """Map a short audience key from config files to its canonical resource."""
        mapping = {
            "orchestrator": self.orchestrator_resource,
            "knowledge": self.knowledge_resource,
            "warehouse": self.warehouse_resource,
            "live_data": self.live_data_resource,
            "actions": self.actions_resource,
            "research_agent": self.research_agent_resource,
        }
        if key not in mapping:
            raise KeyError(f"unknown audience key {key!r}")
        return mapping[key]

    def mcp_url_for(self, key: str) -> str:
        mapping = {
            "knowledge": self.mcp_knowledge_url,
            "warehouse": self.mcp_warehouse_url,
            "live_data": self.mcp_live_data_url,
            "actions": self.mcp_actions_url,
        }
        return mapping[key]


def export_env_file() -> None:
    """Copy .env into the process environment (without overriding real variables).

    Settings read .env themselves, but some libraries only look at os.environ:
    LangSmith tracing, Braintrust, OpenTelemetry. This makes one .env work for all.
    """
    if ENV_FILE.exists():
        for key, value in dotenv_values(ENV_FILE).items():
            if value and key not in os.environ:
                os.environ[key] = value


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    export_env_file()
    return Settings()


def reset_settings_cache() -> None:
    """Tests change environment variables; this makes the next call re-read them."""
    get_settings.cache_clear()
