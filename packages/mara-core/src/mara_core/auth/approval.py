"""One-time approval tokens.

When a person approves an action, the orchestrator signs a short JWT that names
the run, the action, the tool and the SHA-256 of the exact arguments, with a
10-minute expiry and a unique `jti`. The executor and the actions server both
check it, and the actions server refuses a `jti` it has seen before. Change one
character of the arguments and the hash no longer matches, so an approval can
never be stretched to cover something the person did not see.

Ed25519 keeps tokens small and verification fast. Only the orchestrator holds
the private key; the executor and actions server hold the public key.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass

import jwt

from mara_core.auth.keys import load_private_key, load_public_key

ISSUER = "mara-orchestrator"
AUDIENCE = "mara-actions"


class ApprovalError(Exception):
    pass


@dataclass(frozen=True)
class ApprovalClaims:
    run_id: str
    action_id: str
    tool: str
    args_sha256: str
    approver: str
    jti: str
    expires_at: int


class ApprovalSigner:
    def __init__(self, private_key_file: str, ttl_seconds: int = 600):
        self._key = load_private_key(private_key_file)
        self.ttl = ttl_seconds

    def mint(self, *, run_id: str, action_id: str, tool: str, args_sha256: str, approver: str) -> str:
        now = int(time.time())
        return jwt.encode(
            {
                "iss": ISSUER,
                "aud": AUDIENCE,
                "sub": approver,
                "iat": now,
                "exp": now + self.ttl,
                "jti": uuid.uuid4().hex,
                "run_id": run_id,
                "action_id": action_id,
                "tool": tool,
                "args_sha256": args_sha256,
            },
            self._key,
            algorithm="EdDSA",
        )


class ApprovalVerifier:
    def __init__(self, public_key_file: str, leeway: int = 5):
        self._key = load_public_key(public_key_file)
        self.leeway = leeway

    def verify(self, token: str, *, tool: str, args_sha256: str, action_id: str | None = None) -> ApprovalClaims:
        try:
            claims = jwt.decode(
                token,
                self._key,
                algorithms=["EdDSA"],
                audience=AUDIENCE,
                issuer=ISSUER,
                leeway=self.leeway,
                options={"require": ["exp", "jti"]},
            )
        except jwt.PyJWTError as exc:
            raise ApprovalError(f"invalid approval token: {exc}") from exc
        if claims.get("tool") != tool:
            raise ApprovalError("approval was issued for a different tool")
        if claims.get("args_sha256") != args_sha256:
            raise ApprovalError("arguments differ from what was approved")
        if action_id is not None and claims.get("action_id") != action_id:
            raise ApprovalError("approval was issued for a different action")
        return ApprovalClaims(
            run_id=claims["run_id"],
            action_id=claims["action_id"],
            tool=claims["tool"],
            args_sha256=claims["args_sha256"],
            approver=claims.get("sub", ""),
            jti=claims["jti"],
            expires_at=int(claims["exp"]),
        )
