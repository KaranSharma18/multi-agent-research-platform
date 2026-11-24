"""The token broker: one scoped token per task, and nothing standing.

Three kinds of token, each as narrow as it can be:

- task tokens: the user's token exchanged (RFC 8693) for one audience and only
  the scopes `config/scopes.yaml` lists for the task's kind. The user's id and
  groups ride along, so each server applies the user's own permissions;
- catalog tokens: the orchestrator as itself, `catalog:read`, enough for
  `tools/list` and nothing else;
- the A2A token: the orchestrator as itself, `research:run` on the remote
  agent. No user identity ever leaves the platform.

Tokens are cached until shortly before they expire and never logged or stored.
"""

from __future__ import annotations

from dataclasses import dataclass

from mara_core.auth.client import TokenClient
from mara_core.config import scope_policy
from mara_core.settings import Settings


@dataclass(frozen=True)
class TaskToken:
    token: str
    audience: str
    server: str
    scopes: list[str]


class TokenBroker:
    def __init__(self, settings: Settings):
        self.settings = settings
        secret = settings.orchestrator_client_secret.get_secret_value() if settings.orchestrator_client_secret else None
        self.client = TokenClient(
            token_url=settings.auth_token_url,
            client_id=settings.orchestrator_client_id,
            client_secret=secret,
            private_key_file=settings.orchestrator_client_key_file,
        )

    async def task_token(self, user_token: str, kind: str) -> TaskToken:
        policy = scope_policy().task_kinds[kind]
        audience = self.settings.resource_for(policy.audience)
        token = await self.client.exchange(subject_token=user_token, audience=audience, scopes=policy.scopes)
        return TaskToken(token, audience, policy.audience, policy.scopes)

    async def catalog_token(self, server: str) -> str:
        return await self.client.client_credentials(
            audience=self.settings.resource_for(server), scopes=[scope_policy().catalog_scope]
        )

    async def a2a_token(self) -> str:
        policy = scope_policy().task_kinds["web_research"]
        return await self.client.client_credentials(
            audience=self.settings.resource_for(policy.audience), scopes=policy.scopes
        )

    async def aclose(self) -> None:
        await self.client.aclose()
