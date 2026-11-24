"""Canonical JSON and hashing.

Approval tokens are bound to a hash of the action's arguments, and replay keys
tool results by a hash of their arguments. Both only work if the same logical
arguments always produce the same bytes, so everything goes through
`canonical_json`: sorted keys, no whitespace, UTF-8, and strings stripped.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def _normalize(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _normalize(v) for k, v in value.items() if v is not None}
    if isinstance(value, (list, tuple)):
        return [_normalize(v) for v in value]
    if isinstance(value, str):
        return value.strip()
    return value


def canonical_json(value: Any) -> str:
    return json.dumps(_normalize(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def sha256_hex(data: str | bytes) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def args_hash(args: dict[str, Any]) -> str:
    """Hash of tool arguments, stable across key order and stray whitespace."""
    return sha256_hex(canonical_json(args))
