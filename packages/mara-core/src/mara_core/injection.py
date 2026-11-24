"""A heuristic prompt-injection detector.

This is detection, not defense. The architecture is the defense: a plan fixed
before untrusted text is read, task-scoped tokens, typed proposals, argument
allowlists and human approval. This scorer exists to flag the obvious attacks
so they show up in traces and metrics, and so flagged evidence can never be the
only support for a claim.

It needs no model download, which keeps CI and Windows installs simple. You can
swap in a model-based classifier by implementing the same `score()` function.
"""

from __future__ import annotations

import base64
import binascii
import re
from dataclasses import dataclass

_PATTERNS: list[tuple[str, float]] = [
    (
        r"ignore (all |any )?(of )?(the |your |my )?(previous |prior |above |earlier )?(instructions|rules|guidelines|"
        r"system prompt)",
        0.9,
    ),
    (
        r"(instruction|note|message|reminder)s? (for|to) (any |all |the )?(ai|llm|assistant|agent|model|chatbot)s?\b",
        0.7,
    ),
    (r"\bif you are an? (ai|llm|language model|chatbot|ai assistant|ai agent)\b", 0.7),
    (r"(^|\n)\s*(assistant|ai|agent)\s*:", 0.6),
    (r"\b(reveal|print|show|repeat) (your |the )?(system prompt|hidden prompt|instructions)", 0.7),
    (r"\b(report|use|say) that (number|figure|value) instead\b", 0.6),
    (r"disregard (all |any )?(the )?(previous|prior|above|system)", 0.85),
    (r"(you are|act as|pretend to be) (now )?(the |an? )?(system|administrator|admin|developer|root)", 0.7),
    (r"\bsystem\s*:", 0.6),
    (r"(new|updated|override) (instructions|system prompt)", 0.7),
    (r"(pre-?approved|already approved|approval (is )?not (needed|required))", 0.6),
    (
        r"(email|send|forward|post|upload|exfiltrate) (the |all )?(full )?(customer|user|account|contact)s? (list|data|records|table)",
        0.8,
    ),
    (r"(include|paste|append) (the )?(full |entire |whole )?(customer|user|account)s? (list|table|records)", 0.7),
    (r"\b(call|use|invoke|run) (the )?(tool )?`?(run_sql|send_email|create_github_issue|post_slack_message)`?", 0.6),
    (r"\{\s*\"tool\"\s*:", 0.6),
    (r"(close|delete|drop|resolve) (all|every) (the )?(incidents|issues|tables|tickets)", 0.7),
    (r"\bdrop table\b", 0.6),
    (r"<\s*/?\s*(system|instructions?)\s*>", 0.6),
    (r"do not (tell|inform|mention) (the )?(user|human)", 0.6),
    (r"ignora (todas )?las instrucciones|ignorez (toutes )?les instructions|ignoriere (alle )?anweisungen", 0.85),
]
_COMPILED = [(re.compile(p, re.IGNORECASE), w) for p, w in _PATTERNS]
_B64 = re.compile(r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{24,}={0,2}")


@dataclass(frozen=True)
class InjectionVerdict:
    score: float
    flagged: bool
    reasons: tuple[str, ...]


def _decoded_base64_segments(text: str) -> list[str]:
    out = []
    for m in _B64.finditer(text):
        try:
            chunk = m.group(0)
            decoded = base64.b64decode(chunk + "=" * (-len(chunk) % 4), validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError, ValueError):
            continue
        if decoded.isprintable():
            out.append(decoded)
    return out


def score(text: str, threshold: float = 0.5) -> InjectionVerdict:
    if not text:
        return InjectionVerdict(0.0, False, ())
    candidates = [text, *_decoded_base64_segments(text)]
    best, reasons = 0.0, []
    for candidate in candidates:
        for pattern, weight in _COMPILED:
            if pattern.search(candidate):
                reasons.append(pattern.pattern[:48])
                best = max(best, weight)
    # Several independent signals are stronger evidence than one.
    if len(reasons) >= 2:
        best = min(1.0, best + 0.1)
    return InjectionVerdict(round(best, 2), best >= threshold, tuple(reasons[:4]))
