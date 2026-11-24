# 0014. A small dev token service, with Keycloak as the drop-in alternative

**Context.** Zero-trust needs an OAuth server that supports client credentials, token exchange (RFC 8693)
and a JWKS endpoint. Requiring Keycloak for local development means Java, a container and a slow start on
every laptop.

**Decision.** `apps/token-service` is a small FastAPI OAuth server (about 300 lines) driven by
`config/auth/dev-realm.yaml`: users and groups, clients and their grants, and per-client policies for which
audiences and scopes each may request via client credentials or exchange. It issues RS256 JWTs with `iss`,
`sub`, `aud`, `azp`, `scope`, `groups`, `roles` and `act`, and publishes JWKS and discovery metadata.

Every service only depends on standard behaviour (JWKS, `iss`, `aud`, `scope`), so switching to Keycloak is
configuration: `infra/keycloak/realm-mara.json` mirrors the dev realm; see `docs/operations.md`.

**Why.** Fast, transparent, readable, and the token-exchange policy is a few lines of YAML you can read in a
code review.

**Trade-off.** The dev service is for development only: no login UI, no refresh tokens, no key rotation.
Production should use a real identity provider.
