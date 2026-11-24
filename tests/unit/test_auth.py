"""The dev token service and the MCP scope guard: the zero-trust boundary."""

from __future__ import annotations

import json

import httpx
import jwt
import pytest
from mara_core.auth.keys import load_private_key, public_jwk
from mara_core.auth.verify import JWKSCache, Principal
from mara_token_service.app import create_app
from mara_token_service.realm import load_realm

TOKEN_URL = "http://token-service:8090/token"


@pytest.fixture
def token_app(settings):
    return create_app(settings, load_realm())


@pytest.fixture
def client(token_app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=token_app), base_url="http://token-service:8090")


def claims(token: str) -> dict:
    return jwt.decode(token, options={"verify_signature": False})


async def post(client, **form) -> httpx.Response:
    return await client.post("/token", data=form)


async def test_password_grant_gives_an_orchestrator_token(client, settings) -> None:
    resp = await post(client, grant_type="password", client_id="mara-ui", username="alice", password="alice")
    c = claims(resp.json()["access_token"])
    assert c["aud"] == settings.orchestrator_resource and c["sub"] == "alice"
    assert "region:all" in c["groups"] and "user" in c["roles"]
    bad = await post(client, grant_type="password", client_id="mara-ui", username="alice", password="nope")
    assert bad.status_code in (400, 401)


async def test_client_credentials_only_within_policy(client, settings) -> None:
    ok = await post(
        client,
        grant_type="client_credentials",
        client_id="mara-orchestrator",
        client_secret="dev-orchestrator-secret",
        audience="knowledge",
        scope="catalog:read",
    )
    assert claims(ok.json()["access_token"])["aud"] == settings.knowledge_resource
    too_much = await post(
        client,
        grant_type="client_credentials",
        client_id="mara-orchestrator",
        client_secret="dev-orchestrator-secret",
        audience="knowledge",
        scope="docs:read",
    )
    assert too_much.status_code == 400 and too_much.json()["error"] == "invalid_scope"
    wrong_secret = await post(
        client,
        grant_type="client_credentials",
        client_id="mara-orchestrator",
        client_secret="guess",
        audience="knowledge",
        scope="catalog:read",
    )
    assert wrong_secret.status_code == 401


async def test_token_exchange_narrows_to_one_audience_and_keeps_the_user(client, settings) -> None:
    user = (await post(client, grant_type="password", client_id="mara-ui", username="bob", password="bob")).json()[
        "access_token"
    ]
    resp = await post(
        client,
        grant_type="urn:ietf:params:oauth:grant-type:token-exchange",
        client_id="mara-orchestrator",
        client_secret="dev-orchestrator-secret",
        subject_token=user,
        subject_token_type="urn:ietf:params:oauth:token-type:access_token",
        audience=settings.warehouse_resource,
        scope="sql:read",
    )
    c = claims(resp.json()["access_token"])
    assert c["sub"] == "bob" and c["aud"] == settings.warehouse_resource and c["scope"] == "sql:read"
    assert c["azp"] == "mara-orchestrator" and "region:na" in c["groups"]
    beyond = await post(
        client,
        grant_type="urn:ietf:params:oauth:grant-type:token-exchange",
        client_id="mara-orchestrator",
        client_secret="dev-orchestrator-secret",
        subject_token=user,
        subject_token_type="urn:ietf:params:oauth:token-type:access_token",
        audience=settings.actions_resource,
        scope="github:issues:write",
    )
    assert beyond.status_code == 400  # the orchestrator may never get write scopes for a user


def test_principal_reads_keycloak_style_claims() -> None:
    p = Principal.from_claims(
        {
            "sub": "u1",
            "azp": "mara-ui",
            "scope": "a b",
            "realm_access": {"roles": ["approver"]},
            "groups": ["/eng", "/region:emea"],
            "aud": ["x", "y"],
            "preferred_username": "dana",
        }
    )
    assert p.has_role("approver") and p.has_scope("b") and p.username == "dana"
    assert p.acl_groups() == ["all", "eng"] and p.regions() == ["emea"]


async def test_scope_guard_refuses_wrong_scope_and_wrong_audience(client, settings, monkeypatch) -> None:
    """A real MCP server app: the guard answers before the MCP layer runs."""
    from mara_mcp_warehouse.server import create_app as warehouse_app

    public = load_private_key(settings.token_service_key_file).public_key()
    jwks = jwt.PyJWKSet.from_dict({"keys": [public_jwk(public, "RS256")]})

    def fake_fetch(self) -> None:
        import time

        self._keys = {k.key_id: k for k in jwks.keys}
        self._fetched_at = time.monotonic()

    monkeypatch.setattr(JWKSCache, "_fetch", fake_fetch)
    app = warehouse_app(settings)
    mcp = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://mcp-warehouse:8102")
    user = (await post(client, grant_type="password", client_id="mara-ui", username="alice", password="alice")).json()[
        "access_token"
    ]
    docs_token = (
        await post(
            client,
            grant_type="urn:ietf:params:oauth:grant-type:token-exchange",
            client_id="mara-orchestrator",
            client_secret="dev-orchestrator-secret",
            subject_token=user,
            subject_token_type="urn:ietf:params:oauth:token-type:access_token",
            audience=settings.knowledge_resource,
            scope="docs:read",
        )
    ).json()["access_token"]
    catalog = (
        await post(
            client,
            grant_type="client_credentials",
            client_id="mara-orchestrator",
            client_secret="dev-orchestrator-secret",
            audience="warehouse",
            scope="catalog:read",
        )
    ).json()["access_token"]
    call = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "run_sql", "arguments": {"sql": "x"}}}
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}

    wrong_audience = await mcp.post(
        "/mcp", content=json.dumps(call), headers={**headers, "Authorization": f"Bearer {docs_token}"}
    )
    assert wrong_audience.status_code == 401
    no_scope = await mcp.post(
        "/mcp", content=json.dumps(call), headers={**headers, "Authorization": f"Bearer {catalog}"}
    )
    assert no_scope.status_code == 403
    assert "insufficient_scope" in no_scope.headers["www-authenticate"]
    anonymous = await mcp.post("/mcp", content=json.dumps(call), headers=headers)
    assert anonymous.status_code == 401
