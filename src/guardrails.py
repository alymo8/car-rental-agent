"""Pure business-rule checks for an extension request. No I/O.

Each check returns a Verdict whose kind is one of: allow, invalid,
advise_not_needed, escalate.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from config import Settings


@dataclass(frozen=True)
class Verdict:
    kind: str
    reason: str | None = None


ALLOW = Verdict("allow")


def pre_quote(
    current_return: datetime,
    new_return: datetime,
    now: datetime,
    settings: Settings,
    customer_insists: bool = False,
) -> Verdict:
    """Checks before any availability or quote call; first hit wins."""
    if now > current_return + timedelta(minutes=settings.grace_period_minutes):
        return Verdict("escalate", "overdue_rental")
    if new_return <= current_return:
        return Verdict("invalid", "not_later_than_current_return")
    added = new_return - current_return
    if (
        added <= timedelta(minutes=settings.short_extension_minutes)
        and not customer_insists
    ):
        return Verdict("advise_not_needed")
    if added > timedelta(days=settings.max_extension_days):
        return Verdict("escalate", "extension_too_long")
    return ALLOW


def post_quote(charges: dict, settings: Settings) -> Verdict:
    """Checks on the API's quoted charges."""
    if charges["extension_days"] > settings.max_extension_days:
        return Verdict("escalate", "extension_too_long")
    if charges["total_charged"] > settings.max_auto_charge_usd:
        return Verdict("escalate", "high_value")
    return ALLOW


def availability_verdict(availability: dict | None) -> Verdict:
    """Unknown or unavailable requested vehicle type means hand off."""
    if (
        availability is None
        or not availability["availability"]["requested_type"]["available"]
    ):
        return Verdict("escalate", "availability_unconfirmed")
    return ALLOW
