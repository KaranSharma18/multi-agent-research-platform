"""The token endpoint and its discovery documents.

Grants:
  client_credentials   a service as itself (catalog listing, A2A, the executor)
  token-exchange       the orchestrator swapping a user token for a task token
  password             dev only: lets the demo UI and eval harness log users in

Every issued token is an RS256 JWT with iss, sub, aud, azp, scope, exp, iat and
jti. Exchanged tokens keep the user's sub, groups and roles and add an `act`
claim naming the client acting for them (RFC 8693 delegation).
"""

from __future__ import annotations

import hmac
import time
import uuid
from typing import Any

import jwt
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from mara_core.auth.keys import generate_rsa, key_id, load_private_key, load_public_key, public_jwk
from mara_core.logging import get_logger
from mara_core.settings import Settings, get_settings

from mara_token_service.realm import TOKEN_EXCHANGE, Client, Realm, load_realm

log = get_logger(__name__)


class OAuthError(HTTPException):
    def __init__(self, error: str, description: str, status: int = 400):
        super().__init__(status_code=status, detail={"error": error, "error_description": description})


def create_app(settings: Settings | None = None, realm: Realm | None = None) -> FastAPI:
    settings = settings or get_settings()
    realm = realm or load_realm()
    key_path = generate_rsa(settings.token_service_key_file)  # creates a key on first start
    private_key = load_private_key(key_path)
    public_key = private_key.public_key()
    kid = key_id(public_key)
    issuer = settings.auth_issuer

    def resolve_audience(value: str) -> tuple[str, str]:
        """Accept an audience key ('warehouse') or its canonical resource id."""
        for key in ("orchestrator", "knowledge", "warehouse", "live_data", "actions", "research_agent"):
            resource = settings.resource_for(key)
            if value in (key, resource):
                return key, resource
        raise OAuthError("invalid_target", f"unknown audience {value!r}")

    def sign(claims: dict[str, Any], ttl: int) -> dict[str, Any]:
        now = int(time.time())
        claims = {"iss": issuer, "iat": now, "nbf": now, "exp": now + ttl, "jti": uuid.uuid4().hex, **claims}
        token = jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": kid, "typ": "at+jwt"})
        return {
            "access_token": token,
            "token_type": "Bearer",
            "expires_in": ttl,
            "scope": claims.get("scope", ""),
            "issued_token_type": "urn:ietf:params:oauth:token-type:access_token",
        }

    def authenticate_client(request: Request, form: dict[str, str]) -> tuple[str, Client]:
        client_id = form.get("client_id")
        secret = form.get("client_secret")
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("basic "):
            import base64

            decoded = base64.b64decode(auth.split(" ", 1)[1]).decode()
            client_id, _, secret = decoded.partition(":")
        assertion = form.get("client_assertion")
        if assertion:
            client_id = jwt.decode(assertion, options={"verify_signature": False}).get("iss")
        if not client_id or client_id not in realm.clients:
            raise OAuthError("invalid_client", "unknown client", 401)
        client = realm.clients[client_id]
        if client.public:
            return client_id, client
        if assertion:
            if not client.public_key_file:
                raise OAuthError("invalid_client", "client has no registered key", 401)
            try:
                jwt.decode(
                    assertion,
                    load_public_key(client.public_key_file),
                    algorithms=["RS256", "EdDSA"],
                    audience=[str(request.url), settings.auth_token_url, issuer],
                )
            except jwt.PyJWTError as exc:
                raise OAuthError("invalid_client", f"bad client assertion: {exc}", 401) from exc
            return client_id, client
        expected = client.secret()
        if not secret or not expected or not hmac.compare_digest(secret, expected):
            raise OAuthError("invalid_client", "bad client credentials", 401)
        return client_id, client

    def requested_scopes(form: dict[str, str], allowed: list[str]) -> list[str]:
        asked = form.get("scope", "").split()
        if not asked:
            return list(allowed)
        extra = sorted(set(asked) - set(allowed))
        if extra:
            raise OAuthError("invalid_scope", f"not allowed: {' '.join(extra)}")
        return asked

    app = FastAPI(title="MARA dev token service", version="0.1.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in settings.cors_origins.split(",") if o.strip()],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.exception_handler(OAuthError)
    async def oauth_error(_: Request, exc: OAuthError) -> JSONResponse:
        return JSONResponse(exc.detail, status_code=exc.status_code)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/jwks.json")
    async def jwks() -> dict[str, Any]:
        return {"keys": [public_jwk(public_key, "RS256")]}

    @app.get("/.well-known/openid-configuration")
    @app.get("/.well-known/oauth-authorization-server")
    async def discovery() -> dict[str, Any]:
        return {
            "issuer": issuer,
            "token_endpoint": settings.auth_token_url,
            "jwks_uri": settings.auth_jwks_url,
            "grant_types_supported": ["client_credentials", TOKEN_EXCHANGE, "password"],
            "token_endpoint_auth_methods_supported": [
                "client_secret_basic",
                "client_secret_post",
                "private_key_jwt",
                "none",
            ],
            "authorization_response_iss_parameter_supported": True,
        }

    @app.post("/token")
    async def token(request: Request, grant_type: str = Form(...)) -> dict[str, Any]:
        form = {k: str(v) for k, v in (await request.form()).items()}
        client_id, client = authenticate_client(request, form)
        if grant_type not in client.grants:
            raise OAuthError("unauthorized_client", f"{client_id} may not use {grant_type}")

        if grant_type == "client_credentials":
            audience_value = form.get("audience") or form.get("resource")
            if not audience_value:
                raise OAuthError("invalid_request", "audience is required")
            key, resource = resolve_audience(audience_value)
            if key not in client.client_credentials:
                raise OAuthError("invalid_target", f"{client_id} may not request tokens for {key}")
            scopes = requested_scopes(form, client.client_credentials[key])
            log.info("token_issued", grant="client_credentials", client=client_id, audience=key, scopes=scopes)
            return sign(
                {
                    "sub": f"service-account-{client_id}",
                    "aud": resource,
                    "azp": client_id,
                    "client_id": client_id,
                    "scope": " ".join(scopes),
                    "groups": [],
                    "roles": ["service"],
                },
                realm.access_token_ttl_seconds,
            )

        if grant_type == TOKEN_EXCHANGE:
            policy = client.token_exchange
            if policy is None:
                raise OAuthError("unauthorized_client", "token exchange not configured")
            subject = form.get("subject_token")
            if not subject:
                raise OAuthError("invalid_request", "subject_token is required")
            _, subject_aud = resolve_audience(policy.subject_audience)
            try:
                claims = jwt.decode(subject, public_key, algorithms=["RS256"], audience=subject_aud, issuer=issuer)
            except jwt.PyJWTError as exc:
                raise OAuthError("invalid_grant", f"subject token rejected: {exc}") from exc
            key, resource = resolve_audience(form.get("audience") or form.get("resource") or "")
            if key not in policy.audiences:
                raise OAuthError("invalid_target", f"{client_id} may not exchange for {key}")
            scopes = requested_scopes(form, policy.audiences[key])
            log.info(
                "token_issued",
                grant="token_exchange",
                client=client_id,
                user=claims.get("sub"),
                audience=key,
                scopes=scopes,
            )
            return sign(
                {
                    "sub": claims["sub"],
                    "aud": resource,
                    "azp": client_id,
                    "client_id": client_id,
                    "scope": " ".join(scopes),
                    "groups": claims.get("groups", []),
                    "roles": claims.get("roles", []),
                    "preferred_username": claims.get("preferred_username"),
                    "act": {"sub": client_id},
                },
                realm.access_token_ttl_seconds,
            )

        if grant_type == "password":
            username, password = form.get("username", ""), form.get("password", "")
            user = realm.users.get(username)
            if user is None or not hmac.compare_digest(password, user.password):
                raise OAuthError("invalid_grant", "bad username or password")
            _, aud = resolve_audience(client.user_token_audience or "orchestrator")
            log.info("token_issued", grant="password", client=client_id, user=username)
            return sign(
                {
                    "sub": username,
                    "aud": aud,
                    "azp": client_id,
                    "preferred_username": username,
                    "name": user.name,
                    "groups": user.groups,
                    "roles": user.roles,
                    "scope": "openid profile",
                },
                realm.user_token_ttl_seconds,
            )

        raise OAuthError("unsupported_grant_type", grant_type)

    return app
