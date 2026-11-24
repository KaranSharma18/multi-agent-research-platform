"""Verify incoming JWTs and turn their claims into a `Principal`.

Every service does the same four checks on every request: signature (against
the issuer's published keys), issuer, audience (this service's own resource
id) and expiry. Nothing is trusted because of the network a request came from.

Claims differ slightly between Keycloak and the dev token service (roles live
in `realm_access.roles` in Keycloak, groups may carry a leading slash), so
`Principal.from_claims` normalises them into one shape.
"""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
import jwt

ALGORITHMS = ["RS256", "ES256", "EdDSA"]


class AuthError(Exception):
    """Raised when a token is missing, malformed, expired or for someone else."""

    def __init__(self, message: str, *, status: int = 401, error: str = "invalid_token"):
        super().__init__(message)
        self.status = status
        self.error = error


@dataclass(frozen=True)
class Principal:
    subject: str
    client_id: str | None
    scopes: frozenset[str]
    groups: frozenset[str]
    roles: frozenset[str]
    audience: tuple[str, ...]
    expires_at: int | None
    claims: dict[str, Any] = field(repr=False, compare=False)

    @classmethod
    def from_claims(cls, claims: dict[str, Any]) -> Principal:
        scope = claims.get("scope") or ""
        scopes = set(scope.split()) if isinstance(scope, str) else set(scope)
        scopes |= set(claims.get("scp") or [])
        roles = set(claims.get("roles") or [])
        roles |= set((claims.get("realm_access") or {}).get("roles") or [])
        groups = {g.lstrip("/") for g in (claims.get("groups") or [])}
        aud = claims.get("aud") or []
        audience = (aud,) if isinstance(aud, str) else tuple(aud)
        return cls(
            subject=str(claims.get("sub") or claims.get("client_id") or claims.get("azp") or "unknown"),
            client_id=claims.get("azp") or claims.get("client_id"),
            scopes=frozenset(scopes),
            groups=frozenset(groups),
            roles=frozenset(roles),
            audience=audience,
            expires_at=claims.get("exp"),
            claims=claims,
        )

    @property
    def username(self) -> str:
        return str(self.claims.get("preferred_username") or self.subject)

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes

    def has_role(self, role: str) -> bool:
        return role in self.roles

    def acl_groups(self) -> list[str]:
        """Groups that gate document retrieval. Everyone can read `all`."""
        return sorted({"all", *(g for g in self.groups if not g.startswith("region:"))})

    def regions(self) -> list[str]:
        regions = sorted(g.split(":", 1)[1] for g in self.groups if g.startswith("region:"))
        return regions or ["none"]


class JWKSCache:
    """Fetches the issuer's JWKS and refreshes it when an unknown key id appears."""

    def __init__(self, jwks_url: str, refresh_interval: float = 300.0, min_refetch_gap: float = 10.0):
        self.jwks_url = jwks_url
        self.refresh_interval = refresh_interval
        self.min_refetch_gap = min_refetch_gap
        self._keys: dict[str, Any] = {}
        self._fetched_at = 0.0
        self._lock = threading.Lock()

    def _fetch(self) -> None:
        resp = httpx.get(self.jwks_url, timeout=5.0)
        resp.raise_for_status()
        keyset = jwt.PyJWKSet.from_dict(resp.json())
        self._keys = {k.key_id: k for k in keyset.keys if k.key_id}
        self._fetched_at = time.monotonic()

    def get_key(self, kid: str | None):
        with self._lock:
            age = time.monotonic() - self._fetched_at
            if not self._keys or age > self.refresh_interval:
                self._fetch()
            if kid in self._keys:
                return self._keys[kid]
            if age > self.min_refetch_gap:
                self._fetch()
                if kid in self._keys:
                    return self._keys[kid]
            if kid is None and len(self._keys) == 1:
                return next(iter(self._keys.values()))
        raise AuthError(f"unknown signing key {kid!r}")


class JWTVerifier:
    """Checks signature, issuer, audience and expiry. One instance per service."""

    def __init__(self, *, jwks_url: str, issuer: str, audience: str | None, leeway: int = 30):
        self.issuer = issuer
        self.audience = audience
        self.leeway = leeway
        self._jwks = JWKSCache(jwks_url)

    def verify(self, token: str, *, audience: str | None = None) -> Principal:
        if not token:
            raise AuthError("missing bearer token")
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise AuthError(f"malformed token: {exc}") from exc
        try:
            key = self._jwks.get_key(header.get("kid"))
        except httpx.HTTPError as exc:
            raise AuthError(f"cannot fetch issuer keys: {exc}", status=503, error="temporarily_unavailable") from exc
        expected_aud = audience or self.audience
        try:
            claims = jwt.decode(
                token,
                key=key.key,
                algorithms=ALGORITHMS,
                audience=expected_aud,
                issuer=self.issuer,
                leeway=self.leeway,
                options={"require": ["exp", "iat", "sub"], "verify_aud": expected_aud is not None},
            )
        except jwt.ExpiredSignatureError as exc:
            raise AuthError("token expired") from exc
        except jwt.InvalidAudienceError as exc:
            raise AuthError(f"token is not for {expected_aud}", error="invalid_token") from exc
        except jwt.InvalidIssuerError as exc:
            raise AuthError("token from an unexpected issuer") from exc
        except jwt.PyJWTError as exc:
            raise AuthError(f"invalid token: {exc}") from exc
        return Principal.from_claims(claims)

    async def averify(self, token: str, *, audience: str | None = None) -> Principal:
        return await asyncio.to_thread(self.verify, token, audience=audience)


def bearer_from_header(value: str | None) -> str:
    if not value or not value.lower().startswith("bearer "):
        raise AuthError("missing bearer token")
    return value.split(" ", 1)[1].strip()
