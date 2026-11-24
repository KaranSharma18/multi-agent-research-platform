"""Request authentication: every API call carries the user's own bearer token."""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import HTTPException, Request
from mara_core.auth.verify import AuthError, Principal, bearer_from_header


@dataclass(frozen=True)
class User:
    principal: Principal
    token: str

    @property
    def name(self) -> str:
        return self.principal.username

    def has_role(self, role: str) -> bool:
        return self.principal.has_role(role)


async def current_user(request: Request) -> User:
    try:
        token = bearer_from_header(request.headers.get("authorization"))
        principal = await request.app.state.verifier.averify(token)
    except AuthError as exc:
        raise HTTPException(
            exc.status, detail=str(exc), headers={"WWW-Authenticate": f'Bearer error="{exc.error}"'}
        ) from exc
    if not principal.has_role("user"):
        raise HTTPException(403, detail="this account may not use the assistant")
    return User(principal, token)


def can_view_run(user: User, run: dict) -> bool:
    return run["user_sub"] == user.principal.subject or user.has_role("admin") or user.has_role("approver")
