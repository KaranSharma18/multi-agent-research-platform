"""Synthetic data for MARA: the world, the corpus, the warehouse and ingestion."""

from __future__ import annotations

from datetime import date

from mara_core.paths import data_dir
from mara_core.settings import Settings

from mara_data.corpus import build_corpus
from mara_data.fixtures import write_corpus, write_fixtures, write_meta
from mara_data.warehouse import seed_warehouse
from mara_data.world import build_world

META_FILE = "seed_meta.json"


def seed_all(settings: Settings, as_of: date | None = None, seed: int = 42, *, warehouse: bool = True) -> dict:
    """Generate the world and write every artifact: warehouse rows, API fixtures, corpus, metadata."""
    world = build_world(as_of, seed)
    docs = build_corpus(world)
    out = data_dir()
    counts = seed_warehouse(world, settings.warehouse_admin_url) if warehouse else {}
    write_fixtures(world, out / "fixtures")
    write_corpus(docs, out / "corpus")
    write_meta(world, docs, counts, out / META_FILE)
    return {"as_of": world.as_of.isoformat(), "documents": len(docs), **counts}
