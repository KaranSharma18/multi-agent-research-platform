"""Development OAuth 2.0 token service.

Why it exists: the platform's zero-trust design needs real tokens on every call
(task-scoped token exchange, client credentials for services). Keycloak does
that in production, but it is a heavy Java service to stand up on a laptop.
This service speaks the same OAuth requests and issues the same token shape,
so code never knows which one it is talking to.

What it is not: a production identity provider. It has no consent screens, no
refresh tokens, no brute-force protection, and its users live in a YAML file.
"""
