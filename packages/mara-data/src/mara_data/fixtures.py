"""Write the JSON fixtures the mock APIs serve, the corpus files and seed metadata."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from mara_data.corpus import Doc
from mara_data.world import World


def write_fixtures(world: World, out: Path) -> list[Path]:
    out.mkdir(parents=True, exist_ok=True)
    # The incident API does not know root causes: those live in postmortems, as in real life.
    incidents = [{k: v for k, v in inc.items() if k != "root_cause"} for inc in world.incidents]
    files = {
        "incidents.json": incidents,
        "incident_timelines.json": world.timelines,
        "status.json": world.status,
        "github.json": world.github,
        "fx.json": world.fx,
    }
    paths = []
    for name, data in files.items():
        path = out / name
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        paths.append(path)
    return paths


def write_corpus(docs: list[Doc], out: Path) -> int:
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    for doc in docs:
        (out / f"{doc.doc_id}.md").write_text(doc.markdown(), encoding="utf-8")
    return len(docs)


def write_meta(world: World, docs: list[Doc], counts: dict, path: Path) -> None:
    meta = {
        "as_of": world.as_of.isoformat(),
        "seed": world.seed,
        "counts": {**counts, "documents": len(docs)},
        "facts": world.facts,
        "canaries": world.canaries,
        "documents": [
            {
                "doc_id": d.doc_id,
                "title": d.title,
                "source": d.source,
                "acl": d.acl,
                "service": d.service,
                "created_at": d.created_at,
                "tags": d.tags,
            }
            for d in docs
        ],
    }
    path.write_text(json.dumps(meta, indent=2), encoding="utf-8")


def read_meta(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"{path} not found: run `uv run mara seed` first")
    return json.loads(path.read_text(encoding="utf-8"))
