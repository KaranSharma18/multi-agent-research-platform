"""Rendering the versioned prompts in `config/prompts/`.

Two rules keep untrusted text out of the instruction channel:
  - only our own text goes in the system message;
  - anything that came from tools, documents or other agents goes in the human
    message, below a marker, wrapped in <untrusted> elements.
`split()` cuts a rendered prompt at that marker.
"""

from __future__ import annotations

from mara_core.config import load_prompt

PROMPTS = ("planner", "worker", "synthesizer", "verifier", "proposer")


class _Keep(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def render(name: str, **values: object) -> tuple[str, str]:
    text, version = load_prompt(name)
    return text.format_map(_Keep({k: "" if v is None else v for k, v in values.items()})), version


def split(text: str, marker: str) -> tuple[str, str]:
    """(system part, human part). The human part starts at `marker`."""
    head, found, tail = text.partition(marker)
    return head.strip(), (found + tail).strip()


def versions() -> dict[str, str]:
    return {name: load_prompt(name)[1] for name in PROMPTS}
