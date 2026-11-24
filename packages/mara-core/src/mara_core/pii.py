"""Personal-data detection and masking.

Used in four places, always with the same detectors so behaviour matches:
  1. ingestion masks documents before they are indexed,
  2. workers mask tool results before a model sees them (via LangChain's
     PIIMiddleware, which accepts these detectors),
  3. the action proposer blocks outbound arguments that still contain PII,
  4. the eval harness checks that seeded canary values never leak.

Regex detectors are deliberately simple and conservative. They catch the
structured identifiers that matter here (emails, phone numbers, card numbers,
US social security numbers) without a model download. Names are not treated as
PII in this demo: customer company names are business data the agent needs.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import TypedDict


class PIIMatch(TypedDict):
    type: str
    value: str
    start: int
    end: int


EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
# Requires a recognisable phone shape so incident ids, dates and amounts don't match.
PHONE_RE = re.compile(r"(?<![\w-])(?:\+\d{1,3}[\s.-]?)?(?:\(\d{3}\)\s?|\d{3}[\s.-])\d{3}[\s.-]\d{4}(?![\w-])")
SSN_RE = re.compile(r"(?<![\w-])\d{3}-\d{2}-\d{4}(?![\w-])")
CARD_RE = re.compile(r"(?<![\w-])(?:\d[ -]?){13,19}(?![\w-])")


def _luhn_ok(digits: str) -> bool:
    total, parity = 0, len(digits) % 2
    for i, ch in enumerate(digits):
        d = int(ch)
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _finditer(pattern: re.Pattern[str], kind: str, text: str) -> list[PIIMatch]:
    return [PIIMatch(type=kind, value=m.group(0), start=m.start(), end=m.end()) for m in pattern.finditer(text)]


def detect_email(text: str) -> list[PIIMatch]:
    return _finditer(EMAIL_RE, "email", text)


def detect_phone(text: str) -> list[PIIMatch]:
    return _finditer(PHONE_RE, "phone", text)


def detect_ssn(text: str) -> list[PIIMatch]:
    return _finditer(SSN_RE, "ssn", text)


def detect_card(text: str) -> list[PIIMatch]:
    out: list[PIIMatch] = []
    for m in CARD_RE.finditer(text):
        digits = re.sub(r"\D", "", m.group(0))
        if 13 <= len(digits) <= 19 and _luhn_ok(digits):
            out.append(PIIMatch(type="credit_card", value=m.group(0), start=m.start(), end=m.end()))
    return out


DETECTORS: dict[str, Callable[[str], list[PIIMatch]]] = {
    "email": detect_email,
    "phone": detect_phone,
    "ssn": detect_ssn,
    "credit_card": detect_card,
}

PLACEHOLDER = {"email": "[EMAIL]", "phone": "[PHONE]", "ssn": "[SSN]", "credit_card": "[CARD]"}


def find_pii(text: str, kinds: tuple[str, ...] = tuple(DETECTORS)) -> list[PIIMatch]:
    matches: list[PIIMatch] = []
    for kind in kinds:
        matches.extend(DETECTORS[kind](text))
    # Drop overlaps, keeping the earliest and longest match.
    matches.sort(key=lambda m: (m["start"], -(m["end"] - m["start"])))
    kept: list[PIIMatch] = []
    last_end = -1
    for m in matches:
        if m["start"] >= last_end:
            kept.append(m)
            last_end = m["end"]
    return kept


def mask_pii(text: str, kinds: tuple[str, ...] = tuple(DETECTORS)) -> tuple[str, int]:
    """Replace every match with a typed placeholder. Returns (masked_text, count)."""
    matches = find_pii(text, kinds)
    if not matches:
        return text, 0
    out, cursor = [], 0
    for m in matches:
        out.append(text[cursor : m["start"]])
        out.append(PLACEHOLDER.get(m["type"], "[PII]"))
        cursor = m["end"]
    out.append(text[cursor:])
    return "".join(out), len(matches)


def contains_pii(text: str) -> bool:
    return bool(find_pii(text))
