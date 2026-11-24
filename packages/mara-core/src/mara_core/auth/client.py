"""Get tokens from the authorization server.

Works the same against Keycloak and the dev token service because both speak
plain OAuth 2.0:

- `client_credentials`: a service authenticating as itself (the executor, the
  orchestrator's catalog and A2A calls).
- `token_exchange` (RFC 8693): swap a user's token for a narrower one with one
  audience and only the scopes a task needs. This is the core of the
  zero-trust design: every worker call carries one of these.
- `password`: dev and eval convenience only, so a script or the demo UI can log
  in a seeded user without a browser redirect.

Client authentication uses `private_key_jwt` when a key file is configured and
falls back to a client secret otherwise.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
import uuid
from dataclasses import dataclass

import httpx
import jwt

from mara_core.auth.keys import key_id, load_private_key
from mara_core.logging import get_logger

log = get_logger(__name__)

TOKEN_EXCHANGE_GRANT = "urn:ietf:params:oauth:grant-type:token-exchange"
ACCESS_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:access_token"


class TokenError(Exception):
    def __init__(self, message: str, status: int | None = None, body: str | None = None):
        super().__init__(message)
        self.status = status
        self.body = body


@dataclass
class Token:
    access_token: str
    expires_at: float
    scope: str = ""

    def valid_for(self, seconds: float) -> bool:
        return time.time() + seconds < self.expires_at


class TokenClient:
    """Async OAuth client with a small cache. One instance per client identity."""

    def __init__(
        self,
        *,
        token_url: str,
        client_id: str,
        client_secret: str | None = None,
        private_key_file: str | None = None,
        timeout: float = 10.0,
    ):
        self.token_url = token_url
        self.client_id = client_id
        self.client_secret = client_secret
        self._private_key = load_private_key(private_key_file) if private_key_file else None
        self._http = httpx.AsyncClient(timeout=timeout)
        self._cache: dict[str, Token] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def aclose(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------------ grants
    async def client_credentials(self, *, audience: str, scopes: list[str]) -> str:
        key = f"cc|{audience}|{' '.join(sorted(scopes))}"
        return await self._cached(
            key,
            lambda: self._request(
                {
                    "grant_type": "client_credentials",
                    "audience": audience,
                    "resource": audience,
                    "scope": " ".join(scopes),
                }
            ),
        )

    async def exchange(self, *, subject_token: str, audience: str, scopes: list[str]) -> str:
        subject_hash = hashlib.sha256(subject_token.encode()).hexdigest()[:16]
        key = f"tx|{subject_hash}|{audience}|{' '.join(sorted(scopes))}"
        return await self._cached(
            key,
            lambda: self._request(
                {
                    "grant_type": TOKEN_EXCHANGE_GRANT,
                    "subject_token": subject_token,
                    "subject_token_type": ACCESS_TOKEN_TYPE,
                    "requested_token_type": ACCESS_TOKEN_TYPE,
                    "audience": audience,
                    "scope": " ".join(scopes),
                }
            ),
        )

    async def password(self, *, username: str, password: str, scopes: list[str] | None = None) -> str:
        data = {"grant_type": "password", "username": username, "password": password}
        if scopes:
            data["scope"] = " ".join(scopes)
        token = await self._request(data)
        return token.access_token

    # --------------------------------------------------------------- internals
    async def _cached(self, key: str, fetch) -> str:
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            cached = self._cache.get(key)
            if cached and cached.valid_for(30):
                return cached.access_token
            token = await fetch()
            self._cache[key] = token
            return token.access_token

    def _client_auth(self) -> tuple[dict, httpx.BasicAuth | None]:
        if self._private_key is not None:
            now = int(time.time())
            assertion = jwt.encode(
                {
                    "iss": self.client_id,
                    "sub": self.client_id,
                    "aud": self.token_url,
                    "iat": now,
                    "exp": now + 60,
                    "jti": uuid.uuid4().hex,
                },
                self._private_key,
                algorithm="EdDSA" if self._private_key.__class__.__name__.startswith("Ed25519") else "RS256",
                headers={"kid": key_id(self._private_key.public_key())},
            )
            return {
                "client_id": self.client_id,
                "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
                "client_assertion": assertion,
            }, None
        if self.client_secret:
            return {}, httpx.BasicAuth(self.client_id, self.client_secret)
        return {"client_id": self.client_id}, None  # public client

    async def _request(self, data: dict) -> Token:
        extra, auth = self._client_auth()
        payload = {k: v for k, v in {**data, **extra}.items() if v}
        try:
            resp = await self._http.post(self.token_url, data=payload, auth=auth)
        except httpx.HTTPError as exc:
            raise TokenError(f"token endpoint unreachable: {exc}") from exc
        if resp.status_code != 200:
            log.warning(
                "token_request_failed",
                grant=data.get("grant_type"),
                status=resp.status_code,
                audience=data.get("audience"),
            )
            raise TokenError(f"token request failed ({resp.status_code})", resp.status_code, resp.text[:500])
        body = resp.json()
        return Token(
            access_token=body["access_token"],
            expires_at=time.time() + float(body.get("expires_in", 300)),
            scope=body.get("scope", ""),
        )
