"""Token handling shared by every service.

- `verify`: check incoming JWTs (signature, issuer, audience, expiry) against
  the issuer's JWKS, and turn claims into a `Principal`.
- `client`: get tokens from the authorization server (client credentials,
  token exchange, and a dev-only password grant).
- `approval`: mint and check the one-time approval tokens that let the
  executor run exactly one approved action.
- `keys`: generate and load signing keys.
"""

from mara_core.auth.verify import AuthError, JWTVerifier, Principal  # noqa: F401
