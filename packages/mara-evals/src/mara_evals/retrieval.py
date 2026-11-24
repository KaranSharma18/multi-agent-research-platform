"""Retrieval-only eval: recall@k and MRR, straight against the knowledge store.

No models and no orchestrator: this measures the hybrid search (dense +
BM25 sparse, fused with RRF) on its own, so a change in chunking or embeddings
shows up as a number before it shows up as a worse answer. Runs with every ACL
group so permissions do not hide relevant documents.

Stop the knowledge server first when Qdrant runs in embedded mode (a folder):
only one process can open it at a time.
"""

from __future__ import annotations

from mara_core.retrieval.embeddings import make_embedder
from mara_core.settings import get_settings
from mara_mcp_knowledge.store import KnowledgeStore
from rich.console import Console

from mara_evals.datasets import load

console = Console(highlight=False)
ALL_GROUPS = ["all", "eng", "support", "finance"]


async def run_retrieval_eval(k: int = 5) -> dict:
    settings = get_settings()
    store = KnowledgeStore(settings, make_embedder(settings))
    cases = load("retrieval")
    hits = 0
    reciprocal = 0.0
    misses = []
    try:
        for case in cases:
            results = await store.search(case["query"], groups=ALL_GROUPS, top_k=max(k, 10), sources=None)
            docs = list(dict.fromkeys(r["doc_id"] for r in results))
            relevant = set(case["relevant"])
            if relevant & set(docs[:k]):
                hits += 1
            else:
                misses.append((case["query"], case["relevant"], docs[:3]))
            rank = next((i + 1 for i, d in enumerate(docs) if d in relevant), None)
            reciprocal += 1 / rank if rank else 0.0
    finally:
        await store.client.close()
    summary = {
        "cases": len(cases),
        f"recall@{k}": round(hits / len(cases), 4),
        "mrr": round(reciprocal / len(cases), 4),
        "embedder": type(store.embedder).__name__,
    }
    console.print(summary)
    for query, relevant, got in misses[:10]:
        console.print(f"[dim]miss: {query!r} wanted {relevant} got {got}[/]")
    return summary
