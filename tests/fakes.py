"""Test doubles for the HTTP layer: scripted responses and a fake Roadway API, no network."""

from __future__ import annotations

import copy
import json as _json
import random
import re
from collections.abc import Callable
from urllib.parse import urlparse

import requests


class FakeResponse:
    def __init__(self, status_code: int, body: dict | str):
        self.status_code = status_code
        self._body = body

    def json(self):
        if isinstance(self._body, str):
            raise ValueError("not JSON")  # noqa: TRY004 (mirrors requests)
        return self._body

    @property
    def text(self) -> str:
        if isinstance(self._body, str):
            return self._body
        return _json.dumps(self._body)


class ScriptedHTTP:
    """Stands in for requests.Session: returns or raises the next scripted item."""

    def __init__(self, script: list[FakeResponse | Exception]):
        self.script = list(script)
        self.calls: list[dict] = []

    def request(self, method, url, *, headers, params=None, json=None, timeout=None):
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": headers,
                "params": params,
                "json": json,
                "timeout": timeout,
            }
        )
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


# -- fake Roadway API -------------------------------------------------------------

_RESERVATION = {
    "reservation_id": "RWR-29471835",
    "customer_id": "CUST-847291",
    "customer_name": "Sarah Johnson",
    "membership_status": "roadway_preferred",
    "vehicle": {
        "type": "midsize_sedan",
        "description": "Chevrolet Malibu or similar",
        "make_model": "2025 Chevrolet Malibu",
        "color": "Silver",
        "license_plate": "8ABC123",
    },
    "pickup_location": {
        "code": "LAX",
        "name": "Los Angeles International Airport",
        "address": "9217 Airport Blvd, Los Angeles, CA 90045",
    },
    "return_location": {
        "code": "LAX",
        "name": "Los Angeles International Airport",
        "address": "9217 Airport Blvd, Los Angeles, CA 90045",
    },
    "dates": {
        "pickup_datetime": "2027-06-12T10:00:00-07:00",
        "current_return_datetime": "2027-06-15T14:00:00-07:00",
        "original_return_datetime": "2027-06-15T14:00:00-07:00",
    },
    "pricing": {"daily_rate": 45.99, "currency": "USD"},
    "payment": {
        "card_on_file": {"type": "Visa", "last_four": "4832"},
        "total_charged": 137.97,
    },
    "status": "active",
}

DEFAULT_CHARGES = {
    "daily_rate": 45.99,
    "extension_days": 2,
    "subtotal": 91.98,
    "late_fee": 0.0,
    "one_way_fee": 0.0,
    "taxes_and_fees": 8.51,
    "total_charged": 100.49,
    "currency": "USD",
}


def make_reservation(**overrides) -> dict:
    """The docs/api-reference.md `RWR-29471835` example with top-level overrides."""
    reservation = copy.deepcopy(_RESERVATION)
    reservation.update(overrides)
    return reservation


def make_availability(available: bool = True) -> dict:
    """The docs/api-reference.md /availability example; `available` sets the
    requested type's flag."""
    return {
        "location": {"code": "LAX", "name": "Los Angeles International Airport"},
        "requested_vehicle_type": "midsize_sedan",
        "date_range": {"start": "2027-06-15", "end": "2027-06-17"},
        "availability": {
            "requested_type": {
                "vehicle_type": "midsize_sedan",
                "available": available,
                "available_count": 4 if available else 0,
                "daily_rate": 45.99,
                "description": "Chevrolet Malibu or similar",
            },
            "alternative_types_at_location": [
                {
                    "vehicle_type": "compact",
                    "available_count": 7,
                    "daily_rate": 38.99,
                    "description": "Toyota Corolla or similar",
                }
            ],
        },
        "nearby_locations": [
            {
                "code": "SNA",
                "name": "John Wayne Airport (Orange County)",
                "availability": {
                    "midsize_sedan": {
                        "available": True,
                        "available_count": 5,
                        "daily_rate": 45.99,
                    }
                },
            }
        ],
    }


def _error(status: int, code: str, message: str) -> FakeResponse:
    return FakeResponse(
        status, {"success": False, "error": {"code": code, "message": message}}
    )


_RESERVATION_PATH = re.compile(r"^/reservations/([^/]+)(?:/(quote|extend))?$")


class FakeRoadwayServer:
    """Stands in for requests.Session with a small in-memory Roadway API.

    `overrides[endpoint]` queues responses (or exceptions) that win over normal
    routing until the queue is empty. Endpoints: reservation, availability,
    quote, extend_probe (extend without `payment`), extend_payment (with it).
    A 2xx extend is stored per Idempotency-Key and replayed when the key repeats.

    `extend_faults()`, when given, is called before each payment-bearing extend:
    "5xx_before" returns 503 without committing, "timeout_before" raises
    `requests.Timeout` without committing, "timeout_after" handles the request
    (committing, or replaying a committed key) and then raises `requests.Timeout`
    so the response is lost; None handles it normally.
    """

    def __init__(
        self,
        reservations: dict[str, dict],
        emails: dict[str, str],
        availability: dict | Exception | None = None,
        quote_charges: dict | None = None,
        *,
        overrides: dict[str, list[FakeResponse | Exception]] | None = None,
        extend_charges: dict | None = None,
        extend_faults: Callable[[], str | None] | None = None,
    ):
        self.reservations = reservations
        self.emails = emails
        self.availability = availability
        self.quote_charges = {**DEFAULT_CHARGES, **(quote_charges or {})}
        self.extend_charges = (
            {**DEFAULT_CHARGES, **extend_charges}
            if extend_charges is not None
            else dict(self.quote_charges)
        )
        self.overrides = {k: list(v) for k, v in (overrides or {}).items()}
        self.calls: list[dict] = []
        self.commits: list[tuple[str, str | None]] = []
        self._replays: dict[str, FakeResponse] = {}
        self._confirmations = 0
        self.extend_faults = extend_faults

    def request(self, method, url, *, headers, params=None, json=None, timeout=None):
        path = urlparse(url).path
        endpoint, reservation_id = self._endpoint(method, path, json)
        self.calls.append(
            {
                "endpoint": endpoint,
                "method": method,
                "url": url,
                "path": path,
                "headers": headers,
                "params": params,
                "json": json,
                "timeout": timeout,
            }
        )
        fault = None
        if endpoint == "extend_payment" and self.extend_faults is not None:
            fault = self.extend_faults()
            if fault == "5xx_before":
                return _error(503, "SERVICE_UNAVAILABLE", "Injected fault")
            if fault == "timeout_before":
                raise requests.Timeout("injected timeout before commit")
        response = self._handle(endpoint, reservation_id, headers, params, json)
        if fault == "timeout_after":
            raise requests.Timeout("injected timeout after commit")
        return response

    def _handle(self, endpoint, reservation_id, headers, params, json):
        is_extend = endpoint.startswith("extend")
        key = (headers or {}).get("Idempotency-Key")
        if is_extend and key in self._replays:
            return self._replays[key]

        queued = self.overrides.get(endpoint)
        if queued:
            item = queued.pop(0)
            if isinstance(item, Exception):
                raise item
            response = item
        else:
            response = self._route(endpoint, reservation_id, params, json or {})

        if is_extend and 200 <= response.status_code < 300:
            if key:
                self._replays[key] = response
            if endpoint == "extend_payment":
                self.commits.append((reservation_id, key))
        return response

    @staticmethod
    def _endpoint(method: str, path: str, body: dict | None) -> tuple[str, str | None]:
        if method == "GET" and path == "/availability":
            return "availability", None
        match = _RESERVATION_PATH.match(path)
        if match is None:
            return "unknown", None
        reservation_id, action = match.groups()
        if action is None:
            return "reservation", reservation_id
        if action == "quote":
            return "quote", reservation_id
        if body and "payment" in body:
            return "extend_payment", reservation_id
        return "extend_probe", reservation_id

    def _route(self, endpoint, reservation_id, params, body) -> FakeResponse:
        if endpoint == "availability":
            if isinstance(self.availability, Exception):
                raise self.availability
            if self.availability is None:
                return FakeResponse(200, make_availability())
            return FakeResponse(200, copy.deepcopy(self.availability))
        if endpoint == "unknown":
            return _error(404, "NOT_FOUND", "Unknown endpoint")

        reservation = self.reservations.get(reservation_id)
        if reservation is None:
            return _error(404, "RESERVATION_NOT_FOUND", "Reservation not found")
        current_return = reservation["dates"]["current_return_datetime"]
        if endpoint == "reservation":
            return FakeResponse(200, copy.deepcopy(reservation))
        if endpoint == "quote":
            return FakeResponse(
                200,
                {
                    "success": True,
                    "reservation_id": reservation_id,
                    "change_type": "extend",
                    "quote": {
                        "current_return_datetime": current_return,
                        "new_return_datetime": body.get("new_return_datetime"),
                        "charges": dict(self.quote_charges),
                    },
                },
            )

        # extend: email is checked before payment, as the real API does.
        if body.get("email") != self.emails.get(reservation_id):
            return _error(403, "VERIFICATION_FAILED", "Email does not match")
        if "payment" not in body:
            return _error(400, "PAYMENT_VALIDATION_ERROR", "Payment details required")
        if reservation.get("status") != "active":
            return _error(409, "RESERVATION_NOT_ACTIVE", "Reservation is not active")
        self._confirmations += 1
        return FakeResponse(
            200,
            {
                "success": True,
                "reservation_id": reservation_id,
                "confirmation_number": f"EXT-{self._confirmations:06d}",
                "extension_details": {
                    "previous_return_datetime": current_return,
                    "new_return_datetime": body.get("new_return_datetime"),
                    "extension_days": self.extend_charges["extension_days"],
                    "late_return": False,
                },
                "charges": dict(self.extend_charges),
            },
        )


def make_deps(server, tmp_path, now, collector=None):
    """ToolDeps over `server`, logging under `tmp_path`; `now` is a datetime or a
    zero-argument callable returning one."""
    from config import REPO_ROOT, Settings
    from handoff import HandoffWriter
    from kb import KnowledgeBase
    from payment import PaymentDetails, ScriptedPaymentCollector
    from roadway_client import RoadwayClient
    from session import ConversationState, new_session_id
    from telemetry import SessionLogger
    from tools import ToolDeps

    settings = Settings(log_dir=str(tmp_path))
    session_id = new_session_id()
    state = ConversationState(session_id, logger=SessionLogger(session_id, tmp_path))
    client = RoadwayClient(
        "https://roadway.test",
        "test-key",
        settings,
        http=server,
        on_attempt=lambda attempt: state.record("api_attempt", **attempt),
        sleep=lambda s: None,
        rng=random.Random(0),
    )
    kb = KnowledgeBase.load(
        REPO_ROOT / "data" / "knowledge-base" / "articles.json",
        embed=None,
        cache_path=None,
        on_event=state.record,
    )
    if collector is None:
        collector = ScriptedPaymentCollector([PaymentDetails("847", "90210")] * 3)
    clock = now if callable(now) else (lambda: now)
    return ToolDeps(
        state=state,
        client=client,
        payments=collector,
        kb=kb,
        handoffs=HandoffWriter(tmp_path),
        settings=settings,
        now=clock,
    )
