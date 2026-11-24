"""Ingestion: markdown files to masked, scanned, embedded chunks in Qdrant.

Steps per document:
  1. split into chunks along headings (about 700 characters each, with a
     short overlap so a sentence cut at a boundary still appears whole once);
  2. mask personal data, so raw emails, phone numbers, SSNs and card numbers
     never reach the store, the model or a log;
  3. score each chunk for prompt-injection patterns and store the flag, so the
     worker sees `warning="possible-injection"` on that evidence;
  4. embed (Gemini, or the offline hash embedder) and encode the BM25 sparse
     vector; upsert with the document's ACL groups in the payload.

Chunk ids are stable (doc id + position), so re-ingesting replaces chunks
instead of duplicating them.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path

from mara_core import injection
from mara_core.logging import get_logger
from mara_core.pii import mask_pii
from mara_core.retrieval.embeddings import make_embedder
from mara_core.retrieval.sparse import encode_document
from mara_core.settings import Settings
from mara_mcp_knowledge.store import ensure_collection, make_client
from qdrant_client import models

log = get_logger(__name__)
CHUNK_CHARS = 700
OVERLAP_CHARS = 100
NAMESPACE = uuid.UUID("6f0e5a5c-7c0b-4c1e-9a62-6d1c2b9a7a11")


def parse(path: Path) -> tuple[dict, str]:
    text = path.read_text(encoding="utf-8")
    meta: dict = {}
    if text.startswith("---"):
        head, _, body = text[3:].partition("\n---")
        for line in head.strip().splitlines():
            key, _, value = line.partition(":")
            value = value.strip()
            if value.startswith("[") and value.endswith("]"):
                meta[key.strip()] = [v.strip() for v in value[1:-1].split(",") if v.strip()]
            else:
                meta[key.strip()] = value
        text = body.lstrip("-\n")
    return meta, text


def chunk(text: str) -> list[tuple[str, str]]:
    """Return (section path, chunk text) pairs."""
    sections: list[tuple[str, str]] = []
    title, current, lines = "", "", []
    for line in text.splitlines():
        heading = re.match(r"^(#{1,3})\s+(.*)", line)
        if heading:
            if lines:
                sections.append((current, "\n".join(lines).strip()))
            level, name = len(heading.group(1)), heading.group(2).strip()
            if level == 1:
                title, current = name, name
            else:
                current = f"{title} > {name}" if title else name
            lines = [line]
        else:
            lines.append(line)
    if lines:
        sections.append((current, "\n".join(lines).strip()))

    chunks: list[tuple[str, str]] = []
    buffer_path, buffer = "", ""
    for path, body in sections:
        if not body:
            continue
        if len(buffer) + len(body) + 2 <= CHUNK_CHARS:
            buffer = f"{buffer}\n\n{body}".strip()
            buffer_path = buffer_path or path
            continue
        if buffer:
            chunks.append((buffer_path, buffer))
        while len(body) > CHUNK_CHARS:
            cut = body.rfind("\n", 0, CHUNK_CHARS)
            cut = cut if cut > CHUNK_CHARS // 2 else CHUNK_CHARS
            chunks.append((path, body[:cut].strip()))
            body = body[max(cut - OVERLAP_CHARS, 0) :]
        buffer_path, buffer = path, body
    if buffer:
        chunks.append((buffer_path, buffer))
    return chunks


async def ingest(corpus_dir: Path, settings: Settings, *, recreate: bool = True) -> dict:
    files = sorted(corpus_dir.glob("*.md"))  # noqa: ASYNC240 - a batch job; blocking file reads are fine
    if not files:
        raise FileNotFoundError(f"no documents in {corpus_dir}: run `uv run mara seed` first")
    embedder = make_embedder(settings)
    client = make_client(settings.qdrant_url)
    try:
        if recreate and await client.collection_exists(settings.qdrant_collection):
            await client.delete_collection(settings.qdrant_collection)
        await ensure_collection(client, settings.qdrant_collection, embedder.dim)
        points: list[models.PointStruct] = []
        stats = {"documents": 0, "chunks": 0, "pii_masked": 0, "flagged": 0}
        records = []
        for path in files:
            meta, text = parse(path)
            stats["documents"] += 1
            for n, (section, body) in enumerate(chunk(text)):
                masked, pii_count = mask_pii(body)
                verdict = injection.score(masked)
                stats["pii_masked"] += pii_count
                stats["flagged"] += int(verdict.flagged)
                records.append((meta, n, section, masked, verdict.flagged))
        # Embed each chunk with its document title and section in front ("contextual chunk headers"),
        # so a chunk that never names its incident still matches a query about that incident.
        vectors = await embedder.embed_documents([f"{r[0].get('title', '')}\n{r[2]}\n{r[3]}" for r in records])
        for (meta, n, section, masked, flagged), dense in zip(records, vectors, strict=True):
            chunk_id = f"{meta['doc_id']}#{n}"
            idx, vals = encode_document(f"{meta.get('title', '')}\n{section}\n{masked}")
            points.append(
                models.PointStruct(
                    id=str(uuid.uuid5(NAMESPACE, chunk_id)),
                    vector={"dense": dense, "sparse": models.SparseVector(indices=idx, values=vals)},
                    payload={
                        "chunk_id": chunk_id,
                        "doc_id": meta["doc_id"],
                        "title": meta.get("title", meta["doc_id"]),
                        "section_path": section,
                        "source": meta.get("source", "doc"),
                        "url": f"doc://{meta['doc_id']}",
                        "created_at": meta.get("created_at"),
                        "acl": meta.get("acl") or ["all"],
                        "text": masked,
                        "injection_flagged": flagged,
                        "service": meta.get("service"),
                    },
                )
            )
        for start in range(0, len(points), 256):
            await client.upsert(settings.qdrant_collection, points=points[start : start + 256], wait=True)
        stats["chunks"] = len(points)
        log.info("ingest_done", collection=settings.qdrant_collection, embedder=type(embedder).__name__, **stats)
        return stats
    finally:
        await client.close()
