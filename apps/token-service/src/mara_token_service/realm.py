"""Load users and clients from config/auth/dev-realm.yaml."""

from __future__ import annotations

import os
from functools import lru_cache

import yaml
from mara_core.paths import config_dir
from pydantic import BaseModel, Field

TOKEN_EXCHANGE = "urn:ietf:params:oauth:grant-type:token-exchange"


class User(BaseModel):
    password: str
    name: str = ""
    groups: list[str] = Field(default_factory=list)
    roles: list[str] = Field(default_factory=list)


class TokenExchangePolicy(BaseModel):
    subject_audience: str = "orchestrator"
    audiences: dict[str, list[str]] = Field(default_factory=dict)


class Client(BaseModel):
    public: bool = False
    secret_env: str | None = None
    default_secret: str | None = None
    public_key_file: str | None = None  # PEM public key for private_key_jwt client auth
    grants: list[str] = Field(default_factory=list)
    user_token_audience: str | None = None
    client_credentials: dict[str, list[str]] = Field(default_factory=dict)
    token_exchange: TokenExchangePolicy | None = None

    def secret(self) -> str | None:
        if self.secret_env and os.environ.get(self.secret_env):
            return os.environ[self.secret_env]
        return self.default_secret


class Realm(BaseModel):
    access_token_ttl_seconds: int = 300
    user_token_ttl_seconds: int = 900
    users: dict[str, User]
    clients: dict[str, Client]


@lru_cache(maxsize=1)
def load_realm() -> Realm:
    path = os.environ.get("DEV_REALM_FILE") or str(config_dir() / "auth" / "dev-realm.yaml")
    with open(path, encoding="utf-8") as fh:
        return Realm.model_validate(yaml.safe_load(fh))
