"""Calling the remote Web Research Agent over A2A.

What this client checks before trusting anything the agent says:
  1. the Agent Card is signed by the key we pinned (`AGENT_CARD_PUBLIC_KEY_FILE`),
     so a swapped or edited card is refused;
  2. the card's endpoint is on the host we configured, so a card cannot
     redirect our token and data to somewhere else;
  3. we authenticate with a client-credentials token for that agent only
     (`research:run`); no user identity or user token leaves the platform.

The brief that comes back is untrusted input, exactly like a web page: the
research worker turns it into spotlighted, injection-scanned evidence.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import httpx
from a2a.client import A2ACardResolver, ClientConfig, ClientFactory
from a2a.helpers import get_data_parts, get_message_text, new_data_part
from a2a.types import (
    CancelTaskRequest,
    Message,
    Part,
    Role,
    SendMessageConfiguration,
    SendMessageRequest,
    TaskState,
)
from a2a.utils.signing import create_signature_verifier
from google.protobuf.json_format import MessageToDict
from mara_core.auth.keys import key_id, load_public_key
from mara_core.logging import get_logger
from mara_core.paths import resolve
from mara_core.settings import Settings

log = get_logger(__name__)

TERMINAL = {
    TaskState.TASK_STATE_COMPLETED: "completed",
    TaskState.TASK_STATE_FAILED: "failed",
    TaskState.TASK_STATE_CANCELED: "canceled",
    TaskState.TASK_STATE_REJECTED: "rejected",
    TaskState.TASK_STATE_INPUT_REQUIRED: "input_required",
    TaskState.TASK_STATE_AUTH_REQUIRED: "auth_required",
}


@dataclass
class ResearchOutcome:
    status: str  # completed | failed | canceled | rejected | input_required | auth_required | timeout | unavailable
    brief: dict[str, Any] | None = None
    cost_usd: float = 0.0
    message: str = ""
    task_id: str | None = None
    states: list[str] = field(default_factory=list)


class CardRejected(Exception):
    pass


def pinned_key_provider(settings: Settings):
    path = resolve(settings.agent_card_public_key_file)

    def provider(kid: str | None, jku: str | None):
        if not path.exists():
            raise CardRejected(f"no pinned agent-card key at {path}")
        key = load_public_key(path)
        if kid and kid != key_id(key):
            raise CardRejected("agent card signed with an unknown key id")
        return key

    return provider


def _origin(url: str) -> tuple[str, str, int | None]:
    p = urlparse(url)
    return p.scheme, (p.hostname or "").lower(), p.port


class ResearchClient:
    def __init__(self, settings: Settings, broker: Any):
        self.settings = settings
        self.broker = broker

    async def _client(self, http: httpx.AsyncClient):
        resolver = A2ACardResolver(http, self.settings.research_agent_url)
        verifier = create_signature_verifier(pinned_key_provider(self.settings), ["EdDSA", "ES256", "RS256"])
        card = await resolver.get_agent_card(signature_verifier=verifier)
        endpoints = [i.url for i in card.supported_interfaces]
        if not endpoints or any(_origin(u) != _origin(self.settings.research_agent_url) for u in endpoints):
            raise CardRejected(f"agent card points at {endpoints}, expected {self.settings.research_agent_url}")
        config = ClientConfig(
            streaming=True,
            httpx_client=http,
            supported_protocol_bindings=["JSONRPC"],
            accepted_output_modes=["application/json", "text/markdown"],
        )
        return ClientFactory(config).create(card), card

    async def research(
        self, goal: str, *, max_sources: int = 6, recency_days: int = 30, stub: str | None = None, on_state=None
    ) -> ResearchOutcome:
        outcome = ResearchOutcome(status="unavailable")
        try:
            token = await self.broker.a2a_token()
        except Exception as exc:
            outcome.message = f"could not get a token for the research agent: {exc}"[:300]
            return outcome
        timeout = httpx.Timeout(self.settings.a2a_timeout_seconds, connect=5.0)
        async with httpx.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=timeout) as http:
            try:
                client, card = await self._client(http)
            except Exception as exc:  # card fetch, signature or endpoint check failed
                log.warning("a2a_card_rejected", error=str(exc)[:300])
                outcome.message = f"research agent unavailable or untrusted: {exc}"[:300]
                return outcome

            payload: dict[str, Any] = {"goal": goal, "max_sources": max_sources, "recency_days": recency_days}
            if stub:
                payload["stub"] = stub
            message = Message(
                message_id=uuid.uuid4().hex, role=Role.ROLE_USER, parts=[new_data_part(payload), Part(text=goal)]
            )
            request = SendMessageRequest(
                message=message,
                configuration=SendMessageConfiguration(accepted_output_modes=["application/json", "text/markdown"]),
            )
            artifacts = []
            try:
                async with asyncio.timeout(self.settings.a2a_timeout_seconds):
                    async for event in client.send_message(request):
                        kind = event.WhichOneof("payload")
                        status = None
                        if kind == "task":
                            outcome.task_id = event.task.id
                            status = event.task.status
                            artifacts.extend(event.task.artifacts)
                        elif kind == "status_update":
                            outcome.task_id = event.status_update.task_id
                            status = event.status_update.status
                        elif kind == "artifact_update":
                            artifacts.append(event.artifact_update.artifact)
                        elif kind == "message":
                            outcome.status, outcome.message = "completed", get_message_text(event.message)
                        if status is not None:
                            name = TaskState.Name(status.state).removeprefix("TASK_STATE_").lower()
                            outcome.states.append(name)
                            if status.HasField("message"):
                                outcome.message = get_message_text(status.message)
                            if on_state:
                                await on_state(name, outcome.message)
                            if status.state in TERMINAL:
                                outcome.status = TERMINAL[status.state]
            except TimeoutError:
                outcome.status = "timeout"
                outcome.message = f"no result within {self.settings.a2a_timeout_seconds:.0f}s"
                if outcome.task_id:
                    with contextlib.suppress(Exception):  # best effort: the agent may already be gone
                        await client.cancel_task(CancelTaskRequest(id=outcome.task_id))
            except Exception as exc:
                outcome.status = "failed"
                outcome.message = f"A2A call failed: {exc}"[:300]
            finally:
                await client.close()

        for artifact in artifacts:
            data = get_data_parts(list(artifact.parts))
            if data:
                outcome.brief = data[0]
            meta = MessageToDict(artifact.metadata) if artifact.HasField("metadata") else {}
            outcome.cost_usd += float(meta.get("total_cost_usd", 0.0) or 0.0)
        if outcome.status == "completed" and outcome.brief is None:
            outcome.status = "failed"
            outcome.message = outcome.message or "the agent completed without a brief"
        log.info(
            "a2a_research_done",
            status=outcome.status,
            states=outcome.states,
            cost_usd=outcome.cost_usd,
            agent_card=card.name if "card" in locals() else None,
        )
        return outcome
