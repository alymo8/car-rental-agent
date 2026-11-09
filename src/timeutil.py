"""Location time zones and strict local datetime parsing.

Customers type return times as local wall time at the return location; the API
needs explicit UTC offsets. Everything here is pure and network-free.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo

_UTC = timezone.utc

_ZONE_GROUPS: dict[str, tuple[str, ...]] = {
    "America/Los_Angeles": (
        "LAX",
        "SFO",
        "OAK",
        "SNA",
        "SAN",
        "SJC",
        "SMF",
        "BUR",
        "ONT",
        "SEA",
        "PDX",
        "LAS",
    ),
    "America/Phoenix": ("PHX",),
    "America/Denver": ("DEN", "SLC"),
    "America/Chicago": (
        "DFW",
        "IAH",
        "HOU",
        "AUS",
        "SAT",
        "MSY",
        "ORD",
        "MDW",
        "MSP",
        "STL",
    ),
    "America/Detroit": ("DTW",),
    "America/New_York": (
        "ATL",
        "MIA",
        "MCO",
        "TPA",
        "FLL",
        "CLT",
        "JFK",
        "LGA",
        "EWR",
        "BOS",
        "IAD",
        "DCA",
        "BWI",
        "PHL",
    ),
    "America/Anchorage": ("ANC",),
    "Pacific/Honolulu": ("HNL",),
}

LOCATION_ZONES: dict[str, str] = {
    code: zone for zone, codes in _ZONE_GROUPS.items() for code in codes
}

_LOCAL_FORMAT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$")


class InvalidLocalTime(ValueError):
    """A customer-typed local time that cannot be used; `hint` says how to fix it."""

    def __init__(self, hint: str) -> None:
        super().__init__(hint)
        self.hint = hint


def parse_api_datetime(value: str) -> datetime:
    """Parse an API timestamp (`Z` or `±HH:MM`) as aware; naive means UTC."""
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_UTC)
    return dt


def zone_for(location_code: str, pickup_datetime: str) -> tzinfo:
    """Zone for a location code; unmapped codes use the pickup's fixed offset."""
    key = LOCATION_ZONES.get((location_code or "").strip().upper())
    if key:
        return ZoneInfo(key)
    offset = parse_api_datetime(pickup_datetime).utcoffset() or timedelta(0)
    return timezone(offset)


def zone_label(tz: tzinfo) -> str:
    """IANA key for a ZoneInfo, else `UTC±HH:MM`."""
    if isinstance(tz, ZoneInfo):
        return tz.key
    offset = tz.utcoffset(None) or timedelta(0)
    sign = "-" if offset < timedelta(0) else "+"
    minutes = abs(int(offset.total_seconds())) // 60
    return f"UTC{sign}{minutes // 60:02d}:{minutes % 60:02d}"


def _hint(tz: tzinfo) -> str:
    return f"Use YYYY-MM-DDTHH:MM in {zone_label(tz)} local time, e.g. 2027-06-17T14:00"


def parse_local(value: str, tz: tzinfo) -> datetime:
    """Parse `YYYY-MM-DDTHH:MM` as wall time in `tz`.

    A DST-gap (non-existent) wall time raises InvalidLocalTime; a DST-overlap
    (ambiguous) wall time resolves to the earlier instant (fold=0).
    """
    text = (value or "").strip()
    if not _LOCAL_FORMAT.match(text):
        raise InvalidLocalTime(_hint(tz))
    try:
        naive = datetime.strptime(text, "%Y-%m-%dT%H:%M")  # noqa: DTZ007 (localised below)
    except ValueError:
        raise InvalidLocalTime(_hint(tz)) from None
    local = naive.replace(tzinfo=tz, fold=0)
    round_trip = local.astimezone(_UTC).astimezone(tz)
    if round_trip.replace(tzinfo=None) != naive:
        raise InvalidLocalTime(
            f"{text} does not exist in {zone_label(tz)} (clocks change). " + _hint(tz)
        )
    return local


def to_api(dt: datetime, tz: tzinfo) -> str:
    """ISO-8601 string in `tz`, always with an explicit offset."""
    return dt.astimezone(tz).isoformat(timespec="seconds")


def format_local(dt: datetime, tz: tzinfo) -> str:
    """`YYYY-MM-DDTHH:MM` wall time in `tz`."""
    return dt.astimezone(tz).strftime("%Y-%m-%dT%H:%M")
