"""Dense embeddings.

`GeminiEmbedder` calls the Gemini embedding API with a reduced output size
(768 by default) and task types, so documents and queries are embedded for
retrieval. `HashEmbedder` is a deterministic stand-in for tests and offline
demos: no network, no quality, same shape. It is never the default.
"""

from __future__ import annotations

import asyncio
import math
import os
import zlib
from typing import Protocol

from mara_core.retrieval.sparse import tokenize
from mara_core.settings import Settings, get_settings


class Embedder(Protocol):
    dim: int
    model: str

    async def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    async def embed_query(self, text: str) -> list[float]: ...


def _normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


class GeminiEmbedder:
    def __init__(self, *, api_key: str | None, model: str, dim: int, batch_size: int = 64):
        from google import genai

        self._client = genai.Client(api_key=api_key) if api_key else genai.Client()
        self.model = model
        self.dim = dim
        self.batch_size = batch_size

    async def _embed(self, texts: list[str], task_type: str) -> list[list[float]]:
        from google.genai import types

        out: list[list[float]] = []
        for start in range(0, len(texts), self.batch_size):
            batch = texts[start : start + self.batch_size]
            for attempt in range(4):
                try:
                    resp = await self._client.aio.models.embed_content(
                        model=self.model,
                        contents=batch,
                        config=types.EmbedContentConfig(task_type=task_type, output_dimensionality=self.dim),
                    )
                    break
                except Exception:
                    if attempt == 3:
                        raise
                    await asyncio.sleep(2**attempt)
            # Reduced-size Gemini embeddings are not unit length; normalise for cosine.
            out.extend(_normalize(list(e.values)) for e in resp.embeddings)
        return out

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return await self._embed(texts, "RETRIEVAL_DOCUMENT")

    async def embed_query(self, text: str) -> list[float]:
        return (await self._embed([text], "RETRIEVAL_QUERY"))[0]


class HashEmbedder:
    """Feature hashing over tokens. Good enough to exercise the pipeline offline."""

    model = "hash-embedder"

    def __init__(self, dim: int = 256):
        self.dim = dim

    def _vec(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        tokens = tokenize(text)
        for tok in tokens:
            h = zlib.crc32(tok.encode())
            vec[h % self.dim] += 1.0 if (h >> 16) & 1 else -1.0
        for a, b in zip(tokens, tokens[1:], strict=False):
            h = zlib.crc32(f"{a} {b}".encode())
            vec[h % self.dim] += 0.5
        return _normalize(vec)

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vec(t) for t in texts]

    async def embed_query(self, text: str) -> list[float]:
        return self._vec(text)


def make_embedder(settings: Settings | None = None) -> Embedder:
    settings = settings or get_settings()
    if settings.embedding_provider == "hash" or settings.llm_provider == "offline":
        return HashEmbedder(settings.embedding_dim)
    key = settings.google_api_key.get_secret_value() if settings.google_api_key else None
    if not key and not os.environ.get("GOOGLE_GENAI_USE_VERTEXAI"):
        raise RuntimeError(
            "Gemini embeddings need GOOGLE_API_KEY. Set it in .env, or set EMBEDDING_PROVIDER=hash "
            "(and LLM_PROVIDER=offline) to run without any key."
        )
    return GeminiEmbedder(api_key=key, model=settings.embedding_model, dim=settings.embedding_dim)
