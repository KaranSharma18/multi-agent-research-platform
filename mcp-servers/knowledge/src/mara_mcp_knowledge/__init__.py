"""Knowledge MCP server: search postmortems, runbooks and product docs.

Retrieval is hybrid: a dense (semantic) search and a BM25 (exact term) search
run as two Qdrant prefetches and are fused with reciprocal rank fusion. Exact
terms matter here because questions name incident ids, services and error
codes; paraphrased questions need the semantic side. Every search is filtered
by the caller's ACL groups from the token, so the agent can never retrieve a
document its user could not open.
"""
