"""Generate and load signing keys.

Three kinds of key exist in this system:
  - the token service's RSA key (RS256, what Keycloak also uses by default),
  - the orchestrator's Ed25519 key for approval tokens,
  - the research agent's Ed25519 key for signing its Agent Card.

`mara keys generate` writes them to `.secrets/` (git-ignored). Containers mount
that folder read-only. In production these would come from a secret manager.
"""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, rsa

from mara_core.paths import resolve


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def generate_rsa(path: str | Path, overwrite: bool = False) -> Path:
    path = resolve(path)
    if path.exists() and not overwrite:
        return path
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    return path


def generate_ed25519(private_path: str | Path, public_path: str | Path, overwrite: bool = False) -> tuple[Path, Path]:
    private_path, public_path = resolve(private_path), resolve(public_path)
    if private_path.exists() and public_path.exists() and not overwrite:
        return private_path, public_path
    key = ed25519.Ed25519PrivateKey.generate()
    private_path.parent.mkdir(parents=True, exist_ok=True)
    private_path.write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    public_path.write_bytes(
        key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    )
    return private_path, public_path


def load_private_key(path: str | Path):
    return serialization.load_pem_private_key(resolve(path).read_bytes(), password=None)


def load_public_key(path: str | Path):
    return serialization.load_pem_public_key(resolve(path).read_bytes())


def key_id(public_key) -> str:
    """A stable key id: the SHA-256 of the public key, shortened."""
    der = public_key.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return hashlib.sha256(der).hexdigest()[:16]


def public_jwk(public_key, alg: str) -> dict:
    """Export a public key as a JWK (RSA or Ed25519)."""
    if isinstance(public_key, rsa.RSAPublicKey):
        numbers = public_key.public_numbers()
        n = numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8, "big")
        e = numbers.e.to_bytes((numbers.e.bit_length() + 7) // 8, "big")
        return {"kty": "RSA", "use": "sig", "alg": alg, "kid": key_id(public_key), "n": _b64url(n), "e": _b64url(e)}
    if isinstance(public_key, ed25519.Ed25519PublicKey):
        raw = public_key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        return {"kty": "OKP", "crv": "Ed25519", "use": "sig", "alg": alg, "kid": key_id(public_key), "x": _b64url(raw)}
    raise TypeError(f"unsupported key type {type(public_key)!r}")


def jwks_json(public_key, alg: str) -> str:
    return json.dumps({"keys": [public_jwk(public_key, alg)]}, indent=2)
