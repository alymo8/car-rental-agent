"""Redacting JSON-lines session logger.

Payment details (CVV, billing ZIP, the payment block) are dropped at any depth and
every email address is masked before a record is written. Card numbers, CVVs and
ZIPs typed into free text (for example a chat turn) are replaced by `[card]`,
`[cvv]` and `[zip]`.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

EVENTS = {
    "turn_user",
    "turn_agent",
    "tool_call",
    "tool_result",
    "api_attempt",
    "guardrail",
    "kb_search",
    "handoff",
    "error",
}

_DROPPED_KEYS = {"cvv", "billing_zip", "payment"}
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# Card data a customer may type in chat despite the prompt. Card numbers: 13-19
# digits, contiguous or in card-style groups (4-4-4-1..7, 4-4-4-4-3, Amex 4-6-5);
# grouping keeps two adjacent dates (8 digits each) from looking like a card.
_CARD_RE = re.compile(
    r"(?<![\w-])(?:\d{13,19}"
    r"|\d{4}([ -])\d{4}\1\d{4}\1\d{4}\1\d{3}"
    r"|\d{4}([ -])\d{4}\2\d{4}\2\d{1,7}"
    r"|\d{4}([ -])\d{6}\3\d{5})(?![\w-])"
)
_LEAD = r"\b[\s:#=.\-]*(?:is\s+)?"
_CVV_RE = re.compile(r"(?i)(\b(?:cvv2|cvv|cvc|security\s+code)" + _LEAD + r")\d{3,4}\b")
_ZIP_RE = re.compile(
    r"(?i)(\b(?:billing\s+zip(?:\s+code)?|zip(?:\s+code)?|postal\s+code)"
    + _LEAD
    + r")\d{5}(?:-\d{4})?\b"
)


def mask_email(email: str) -> str:
    """`david.kim@example.com` -> `d***@example.com`."""
    local, _, domain = email.partition("@")
    return f"{local[:1]}***@{domain}"


def _mask_text(text: str) -> str:
    text = _EMAIL_RE.sub(lambda m: mask_email(m.group(0)), text)
    text = _CARD_RE.sub("[card]", text)
    text = _CVV_RE.sub(r"\1[cvv]", text)
    return _ZIP_RE.sub(r"\1[zip]", text)


def redact(value: Any) -> Any:
    """Return a redacted copy of `value` (dicts, lists, tuples and strings recursed)."""
    if isinstance(value, dict):
        return {
            k: redact(v)
            for k, v in value.items()
            if not (isinstance(k, str) and k.lower() in _DROPPED_KEYS)
        }
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    if isinstance(value, str):
        return _mask_text(value)
    return value


class SessionLogger:
    """Appends redacted events to `<log_dir>/sessions/<session_id>.jsonl`."""

    def __init__(self, session_id: str, log_dir: Path) -> None:
        self.session_id = session_id
        self.path = Path(log_dir) / "sessions" / f"{session_id}.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, event: str, level: str = "info", **fields: Any) -> dict:
        if event not in EVENTS:
            raise ValueError(f"unknown log event: {event!r}")
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "session_id": self.session_id,
            "event": event,
            "level": level,
            **fields,
        }
        record = redact(record)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")
        return record
