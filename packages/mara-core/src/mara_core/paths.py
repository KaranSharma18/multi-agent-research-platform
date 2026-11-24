"""Find the repo's config and data folders without hard-coding absolute paths.

Services run from the repo root during development, from /app inside Docker,
and from a test's temp directory in CI. MARA_CONFIG_DIR and MARA_DATA_DIR win
when set; otherwise we walk up from the current directory until we find a
`config/scopes.yaml`, which only the repo root has.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def repo_root() -> Path:
    env = os.environ.get("MARA_ROOT")
    if env:
        return Path(env).resolve()
    here = Path.cwd().resolve()
    for candidate in (here, *here.parents):
        if (candidate / "config" / "scopes.yaml").is_file():
            return candidate
    # Fall back to the location of this file: packages/mara-core/src/mara_core/paths.py
    fallback = Path(__file__).resolve().parents[4]
    if (fallback / "config" / "scopes.yaml").is_file():
        return fallback
    return here


def config_dir() -> Path:
    env = os.environ.get("MARA_CONFIG_DIR")
    return Path(env).resolve() if env else repo_root() / "config"


def data_dir() -> Path:
    env = os.environ.get("MARA_DATA_DIR")
    path = Path(env).resolve() if env else repo_root() / "data" / "generated"
    path.mkdir(parents=True, exist_ok=True)
    return path


def secrets_dir() -> Path:
    env = os.environ.get("MARA_SECRETS_DIR")
    path = Path(env).resolve() if env else repo_root() / ".secrets"
    path.mkdir(parents=True, exist_ok=True)
    return path


def resolve(path: str | Path) -> Path:
    """Resolve a path relative to the repo root (absolute paths pass through)."""
    p = Path(path)
    return p if p.is_absolute() else repo_root() / p
