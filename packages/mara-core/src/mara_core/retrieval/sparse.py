"""BM25-style sparse vectors for Qdrant, with no model download.

Qdrant computes the IDF part of BM25 itself when the sparse vector is created
with `modifier=IDF`. Our job is the term-frequency part: tokenize, hash each
term to an index, and weight it with BM25's saturation formula. This is what
FastEmbed's BM25 model does, minus the download, which matters on locked-down
networks and in CI.

Tokens keep identifiers such as `INC-2041`, `ERR_TIMEOUT` and `payments-api`
whole, because exact matches on those are the reason hybrid search exists.
"""

from __future__ import annotations

import re
import zlib
from collections import Counter

_TOKEN = re.compile(r"[A-Za-z]+(?:[-_][A-Za-z0-9]+)+|[A-Za-z0-9]+(?:[-_][A-Za-z0-9]+)*")
_STOP = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "but",
        "by",
        "for",
        "from",
        "has",
        "have",
        "in",
        "into",
        "is",
        "it",
        "its",
        "of",
        "on",
        "or",
        "that",
        "the",
        "this",
        "to",
        "was",
        "were",
        "will",
        "with",
        "what",
        "which",
        "who",
        "how",
        "when",
        "where",
        "why",
        "do",
        "does",
        "did",
        "can",
        "could",
        "should",
        "would",
        "our",
        "we",
        "you",
        "your",
        "they",
        "their",
        "there",
    ]
)
INDEX_SPACE = 1 << 20
K1, B, AVG_DOC_LEN = 1.2, 0.75, 120.0


def _stem(token: str) -> str:
    if "-" in token or "_" in token or any(ch.isdigit() for ch in token):
        return token
    for suffix in ("ingly", "edly", "ing", "ies", "ied", "es", "ed", "ly", "s"):
        if token.endswith(suffix) and len(token) - len(suffix) >= 3:
            return token[: -len(suffix)] + ("y" if suffix in ("ies", "ied") else "")
    return token


def tokenize(text: str) -> list[str]:
    tokens = []
    for match in _TOKEN.finditer(text.lower()):
        tok = match.group(0)
        if tok in _STOP or len(tok) < 2:
            continue
        tokens.append(_stem(tok))
    return tokens


def _index(term: str) -> int:
    return zlib.crc32(term.encode("utf-8")) % INDEX_SPACE


def encode_document(text: str) -> tuple[list[int], list[float]]:
    tokens = tokenize(text)
    if not tokens:
        return [], []
    counts = Counter(tokens)
    length_norm = 1 - B + B * (len(tokens) / AVG_DOC_LEN)
    weights: dict[int, float] = {}
    for term, tf in counts.items():
        idx = _index(term)
        weights[idx] = weights.get(idx, 0.0) + tf * (K1 + 1) / (tf + K1 * length_norm)
    indices = sorted(weights)
    return indices, [round(weights[i], 4) for i in indices]


def encode_query(text: str) -> tuple[list[int], list[float]]:
    indices = sorted({_index(t) for t in tokenize(text)})
    return indices, [1.0] * len(indices)
