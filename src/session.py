"""Per-conversation state: verification, quotes, handoffs and the redacted trace."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone, tzinfo
from typing import Any

from telemetry import SessionLogger, redact

QUOTE_STATUSES = (
    "presented",
    "superseded",
    "declined",
    "in_flight",  # the charge request is being sent; outcome not yet known
    "committed",
    "unknown",
    "voided",
)
# A quote in one of these states may have charged the card: no further quote or
# charge in the session (a new quote would mean a new key and a second charge).
CHARGE_PENDING_STATUSES = ("in_flight", "unknown")


@dataclass
class Quote:
    token: str
    new_return_utc: datetime
    new_return_local: str
    charges: dict
    created_at: datetime
    status: str = "presented"
    attempts: int = 0
    charge_key: str | None = None  # idempotency key of the latest charge attempt
    committed: dict | None = None  # confirmation summary once committed


@dataclass
class ConversationState:
    session_id: str
    logger: SessionLogger | None = None
    verification: str = "unverified"  # unverified | verified | email_unconfirmed
    reservation_id: str | None = None
    email: str | None = None
    reservation: dict | None = None
    zone: tzinfo | None = None
    email_failures: int = 0
    payment_failures: int = 0
    verify_attempts: int = 0
    quotes: list[Quote] = field(default_factory=list)
    handoffs: dict[str, str] = field(default_factory=dict)  # reason -> reference
    handoff_count: int = 0
    trace: list[dict] = field(default_factory=list)

    def record(self, event: str, level: str = "info", **fields: Any) -> None:
        """Log an event (redacted) and append the redacted record to the trace."""
        if self.logger is not None:
            rec = self.logger.write(event, level, **fields)
        else:
            rec = redact(
                {
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "session_id": self.session_id,
                    "event": event,
                    "level": level,
                    **fields,
                }
            )
        self.trace.append(rec)

    def add_quote(self, quote: Quote) -> None:
        """Supersede every earlier presented quote, then append `quote`."""
        for q in self.quotes:
            if q.status == "presented":
                q.status = "superseded"
        self.quotes.append(quote)

    def latest_quote(self) -> Quote | None:
        return self.quotes[-1] if self.quotes else None

    def is_verified(self) -> bool:
        return self.verification in ("verified", "email_unconfirmed")


def new_session_id() -> str:
    return uuid.uuid4().hex[:8]
