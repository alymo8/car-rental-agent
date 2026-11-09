"""Structured human-handoff records, deduplicated per reason within a session."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from session import ConversationState
from telemetry import mask_email, redact


class HandoffWriter:
    """Writes `<log_dir>/handoffs/<session_id>-<n>.json`."""

    def __init__(self, log_dir: Path) -> None:
        self.log_dir = Path(log_dir)

    def open(
        self,
        state: ConversationState,
        reason: str,
        summary: str,
        *,
        idempotency_key: str | None = None,
        charge_status: str | None = None,
        now: datetime | None = None,
    ) -> dict:
        directory = self.log_dir / "handoffs"
        if reason in state.handoffs:
            reference = state.handoffs[reason]
            n = reference.rsplit("-", 1)[-1]
            return {
                "handoff_reference": reference,
                "path": str(directory / f"{state.session_id}-{n}.json"),
                "already_open": True,
            }

        state.handoff_count += 1
        n = state.handoff_count
        reference = f"HO-{state.session_id}-{n}"
        quote = state.latest_quote()
        timestamp = now or datetime.now(timezone.utc)
        record = {
            "reference": reference,
            "timestamp": timestamp.isoformat(),
            "reason": reason,
            "summary": summary,
            "reservation_id": state.reservation_id,
            "verified": state.is_verified(),
            "email_masked": mask_email(state.email) if state.email else None,
            "latest_quote": (
                {
                    "token": quote.token,
                    "new_return_local": quote.new_return_local,
                    "charges": quote.charges,
                    "status": quote.status,
                }
                if quote
                else None
            ),
            "idempotency_key": idempotency_key,
            "charge_status": charge_status,
            "recent_events": state.trace[-10:],
        }
        record = redact(record)

        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{state.session_id}-{n}.json"
        path.write_text(json.dumps(record, indent=2, default=str), encoding="utf-8")

        state.handoffs[reason] = reference
        state.record("handoff", reason=reason, reference=reference)
        return {
            "handoff_reference": reference,
            "path": str(path),
            "already_open": False,
        }
