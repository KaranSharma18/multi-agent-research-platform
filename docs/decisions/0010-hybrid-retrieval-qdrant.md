# 0010. Hybrid retrieval in Qdrant with ACL filters

**Context.** Questions mix exact tokens ("INC-2041", "deployctl") with paraphrases ("how do I undo a bad
deploy"). Dense vectors miss the first kind; keyword search misses the second.

**Decision.**
- Chunks of about 700 characters along headings, embedded with the document title and section in front
  (contextual chunk headers), with `gemini-embedding-001` at 768 dimensions.
- A BM25-style sparse vector per chunk (our own tokenizer keeps ids like `INC-2041` whole; Qdrant applies
  IDF), stored alongside the dense vector in one collection.
- Queries prefetch 40 from each, fuse with Reciprocal Rank Fusion, keep 20, optionally rerank with the small
  model, and return `top_k`.
- Every query carries a filter on the `acl` payload built from the token's groups. Filtering happens inside
  Qdrant, before ranking, so restricted chunks can't leak through scores or counts.
- Personal data is masked and injection scored at ingestion; chunk ids are stable so re-ingesting replaces.

**Why.** Hybrid with RRF is a strong, tuning-free default, and the retrieval eval (recall@5, MRR) measures
it without any model in the loop.

**Trade-off.** Qdrant is one more store. Embedded mode (`QDRANT_URL=data/generated/qdrant`) removes the
server for local work, at the cost of single-process access.
