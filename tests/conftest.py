"""Shared fixtures. Unit tests never touch the network, a database or your .env."""

from __future__ import annotations

from pathlib import Path

import pytest
from mara_core.auth.keys import generate_ed25519, generate_rsa
from mara_core.settings import Settings


@pytest.fixture
def keys(tmp_path: Path) -> dict[str, str]:
    rsa = generate_rsa(tmp_path / "token.pem")
    approval_priv, approval_pub = generate_ed25519(tmp_path / "approval.pem", tmp_path / "approval.pub.pem")
    card_priv, card_pub = generate_ed25519(tmp_path / "card.pem", tmp_path / "card.pub.pem")
    return {
        "rsa": str(rsa),
        "approval_private": str(approval_priv),
        "approval_public": str(approval_pub),
        "card_private": str(card_priv),
        "card_public": str(card_pub),
    }


@pytest.fixture
def settings(keys: dict[str, str]) -> Settings:
    """Settings that ignore the developer's .env file."""
    return Settings(
        _env_file=None,
        llm_provider="offline",
        embedding_provider="hash",
        token_service_key_file=keys["rsa"],
        approval_private_key_file=keys["approval_private"],
        approval_public_key_file=keys["approval_public"],
        agent_card_private_key_file=keys["card_private"],
        agent_card_public_key_file=keys["card_public"],
        auth_issuer="http://token-service:8090",
        auth_token_url="http://token-service:8090/token",
        auth_jwks_url="http://token-service:8090/jwks.json",
    )
