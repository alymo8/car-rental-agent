from datetime import datetime, timedelta, timezone

import requests
from fakes import (
    FakeResponse,
    FakeRoadwayServer,
    make_availability,
    make_deps,
    make_reservation,
)

from tools import quote_extension_impl, verify_customer_impl

PDT = timezone(timedelta(hours=-7))
NOW = datetime(2027, 6, 14, 12, 0, tzinfo=PDT)
RID = "RWR-29471835"
EMAIL = "sarah.johnson@example.com"
BOOKING_KEYS = {
    "customer_name",
    "membership_tier",
    "vehicle",
    "vehicle_type",
    "return_location",
    "current_return_local",
    "daily_rate",
    "card_type",
    "card_last_four",
}


def _error(status, code, message="error"):
    return FakeResponse(
        status, {"success": False, "error": {"code": code, "message": message}}
    )


def _server(reservation=None, **kw):
    reservation = reservation or make_reservation()
    rid = reservation["reservation_id"]
    return FakeRoadwayServer({rid: reservation}, {rid: EMAIL}, **kw)


def _verified(tmp_path, now=NOW, **kw):
    server = _server(**kw)
    deps = make_deps(server, tmp_path, now)
    assert verify_customer_impl(deps, RID, EMAIL)["outcome"] == "verified"
    return server, deps


def _calls(server, endpoint):
    return [c for c in server.calls if c["endpoint"] == endpoint]


def _handoff_files(tmp_path):
    directory = tmp_path / "handoffs"
    return list(directory.glob("*.json")) if directory.exists() else []


# -- verify_customer ---------------------------------------------------------


def test_verify_success_returns_summary(tmp_path):
    server = _server()
    deps = make_deps(server, tmp_path, NOW)
    result = verify_customer_impl(deps, RID, EMAIL)
    assert result["outcome"] == "verified"
    assert result["email_confirmed"] is True
    assert result["card_last_four"] == "4832"
    assert result["current_return_local"] == "2027-06-15T14:00"
    assert result["return_location"]["time_zone"] == "America/Los_Angeles"
    (probe,) = _calls(server, "extend_probe")
    assert "payment" not in probe["json"]
    assert probe["headers"]["Idempotency-Key"] == f"verify-{deps.state.session_id}-1"


def test_verify_normalises_inputs(tmp_path):
    deps = make_deps(_server(), tmp_path, NOW)
    result = verify_customer_impl(deps, " rwr-29471835 ", "Sarah.Johnson@Example.com ")
    assert result["outcome"] == "verified"


def test_verify_wrong_email_then_handoff(tmp_path):
    deps = make_deps(_server(), tmp_path, NOW)
    first = verify_customer_impl(deps, RID, "wrong@example.com")
    assert first["outcome"] == "not_verified"
    assert first["attempts_remaining"] == 1
    assert not BOOKING_KEYS & set(first)
    second = verify_customer_impl(deps, RID, "wrong@example.com")
    assert second["outcome"] == "escalated"
    assert second["reason"] == "verification_failed"
    assert len(_handoff_files(tmp_path)) == 1


def test_verify_404_same_as_wrong_email(tmp_path):
    deps = make_deps(_server(), tmp_path, NOW)
    result = verify_customer_impl(deps, "RWR-00000000", EMAIL)
    assert result["outcome"] == "not_verified"
    assert deps.state.email_failures == 1


def test_verify_not_active(tmp_path):
    deps = make_deps(_server(make_reservation(status="completed")), tmp_path, NOW)
    result = verify_customer_impl(deps, RID, EMAIL)
    assert result["outcome"] == "not_eligible"
    assert "customer_name" not in result


def test_verify_other_4xx_falls_back(tmp_path):
    server = _server(overrides={"extend_probe": [_error(400, "INVALID_EXTENSION")]})
    deps = make_deps(server, tmp_path, NOW)
    result = verify_customer_impl(deps, RID, EMAIL)
    assert result["outcome"] == "verified"
    assert result["email_confirmed"] is False
    assert deps.state.verification == "email_unconfirmed"


def test_verify_email_unconfirmed_withholds_booking_details(tmp_path):
    server = _server(overrides={"extend_probe": [_error(400, "INVALID_EXTENSION")]})
    deps = make_deps(server, tmp_path, NOW)
    result = verify_customer_impl(deps, RID, EMAIL)
    assert result == {
        "outcome": "verified",
        "email_confirmed": False,
        "time_zone": "America/Los_Angeles",
        "current_return_local": "2027-06-15T14:00",
        "now_local": "2027-06-14T12:00",
    }


def test_verify_get_5xx_system_unavailable(tmp_path):
    server = _server(
        overrides={"reservation": [_error(503, "SERVICE_UNAVAILABLE")] * 3}
    )
    deps = make_deps(server, tmp_path, NOW)
    result = verify_customer_impl(deps, RID, EMAIL)
    assert result["outcome"] == "system_unavailable"
    assert deps.state.email_failures == 0
    assert _handoff_files(tmp_path) == []


def test_verify_probe_2xx_escalates(tmp_path):
    success = {
        "success": True,
        "reservation_id": RID,
        "confirmation_number": "EXT-1",
        "extension_details": {"extension_days": 1, "late_return": False},
        "charges": {"total_charged": 50.0, "currency": "USD"},
    }
    server = _server(overrides={"extend_probe": [FakeResponse(200, success)]})
    deps = make_deps(server, tmp_path, NOW)
    result = verify_customer_impl(deps, RID, EMAIL)
    assert result["outcome"] == "escalated"
    assert result["reason"] == "unexpected_charge_on_verification"


def test_verify_unmapped_location_uses_pickup_offset(tmp_path):
    reservation = make_reservation()
    reservation["return_location"]["code"] = "ZZZ"
    reservation["dates"]["pickup_datetime"] = "2027-06-12T10:00:00-05:00"
    reservation["dates"]["current_return_datetime"] = "2027-06-15T19:00:00Z"
    deps = make_deps(_server(reservation), tmp_path, NOW)
    result = verify_customer_impl(deps, RID, EMAIL)
    assert result["current_return_local"] == "2027-06-15T14:00"
    assert result["return_location"]["time_zone"] == "UTC-05:00"


def test_failed_reverify_on_other_booking_clears_verification(tmp_path):
    other = "RWR-11111111"
    server = FakeRoadwayServer(
        {RID: make_reservation(), other: make_reservation(reservation_id=other)},
        {RID: EMAIL, other: "other@example.com"},
    )
    deps = make_deps(server, tmp_path, NOW)
    assert verify_customer_impl(deps, RID, EMAIL)["outcome"] == "verified"
    presented = quote_extension_impl(deps, "2027-06-17T14:00")
    assert presented["outcome"] == "quoted"
    assert verify_customer_impl(deps, other, EMAIL)["outcome"] == "not_verified"
    server.calls.clear()
    result = quote_extension_impl(deps, "2027-06-17T14:00")
    assert result["outcome"] == "verification_required"
    assert _calls(server, "quote") == []
    assert deps.state.quotes[0].status == "voided"


# -- quote_extension ---------------------------------------------------------


def test_quote_requires_verification(tmp_path):
    server = _server()
    deps = make_deps(server, tmp_path, NOW)
    result = quote_extension_impl(deps, "2027-06-17T14:00")
    assert result["outcome"] == "verification_required"
    assert _calls(server, "quote") == []


def test_quote_rejects_bad_format(tmp_path):
    server, deps = _verified(tmp_path)
    for text in ("June 17 at 2pm", "2027-06-17 14:00"):
        result = quote_extension_impl(deps, text)
        assert result["outcome"] == "invalid_datetime"
        assert "YYYY-MM-DDTHH:MM" in result["hint"]
        assert "America/Los_Angeles" in result["hint"]
    assert _calls(server, "availability") == []
    assert _calls(server, "quote") == []


def test_quote_happy(tmp_path):
    server, deps = _verified(tmp_path)
    result = quote_extension_impl(deps, "2027-06-17T14:00")
    assert result["outcome"] == "quoted"
    assert result["total_charged"] == 100.49
    assert result["quote_token"] == "q1"
    (quote_call,) = _calls(server, "quote")
    assert quote_call["json"]["new_return_datetime"] == "2027-06-17T14:00:00-07:00"
    (avail_call,) = _calls(server, "availability")
    assert avail_call["params"]["start_date"] == "2027-06-15"
    assert avail_call["params"]["end_date"] == "2027-06-17"
    assert any(
        e["event"] == "guardrail"
        and e["check"] == "availability"
        and e["verdict"] == "allow"
        for e in deps.state.trace
    )


def test_quote_not_later_invalid_request(tmp_path):
    server, deps = _verified(tmp_path)
    result = quote_extension_impl(deps, "2027-06-15T13:00")
    assert result["outcome"] == "invalid_request"
    assert _handoff_files(tmp_path) == []
    assert _calls(server, "quote") == []


def test_quote_short_extension_advised(tmp_path):
    server, deps = _verified(tmp_path)
    result = quote_extension_impl(deps, "2027-06-15T14:20")
    assert result["outcome"] == "advise_not_needed"
    assert _calls(server, "availability") == []
    insisted = quote_extension_impl(deps, "2027-06-15T14:20", customer_insists=True)
    assert insisted["outcome"] == "quoted"


def test_quote_overdue_escalates(tmp_path):
    overdue_now = datetime(2027, 6, 15, 14, 31, tzinfo=PDT)
    server, deps = _verified(tmp_path, now=overdue_now)
    result = quote_extension_impl(deps, "2027-06-17T14:00")
    assert result["outcome"] == "escalated"
    assert result["reason"] == "overdue_rental"
    assert _calls(server, "availability") == []


def test_quote_too_long_escalates(tmp_path):
    _, deps = _verified(tmp_path)
    result = quote_extension_impl(deps, "2027-06-30T14:00")
    assert result["outcome"] == "escalated"
    assert result["reason"] == "extension_too_long"


def test_quote_high_value_escalates(tmp_path):
    _, deps = _verified(tmp_path, quote_charges={"total_charged": 500.01})
    result = quote_extension_impl(deps, "2027-06-17T14:00")
    assert result["outcome"] == "escalated"
    assert result["reason"] == "high_value"
    assert not [q for q in deps.state.quotes if q.status == "presented"]


def test_quote_availability_unknown_escalates(tmp_path):
    _, deps = _verified(tmp_path, availability=requests.Timeout())
    result = quote_extension_impl(deps, "2027-06-17T14:00")
    assert result["outcome"] == "escalated"
    assert result["reason"] == "availability_unconfirmed"


def test_quote_unavailable_escalates_with_alternatives(tmp_path):
    _, deps = _verified(tmp_path, availability=make_availability(available=False))
    result = quote_extension_impl(deps, "2027-06-17T14:00")
    assert result["outcome"] == "escalated"
    assert result["reason"] == "availability_unconfirmed"
    assert result["alternatives"]


def test_quote_4xx_and_5xx(tmp_path):
    _, deps = _verified(
        tmp_path / "a", overrides={"quote": [_error(400, "INVALID_DATE")]}
    )
    failed = quote_extension_impl(deps, "2027-06-17T14:00")
    assert failed["outcome"] == "quote_failed"
    assert failed["code"] == "INVALID_DATE"
    assert deps.state.handoffs == {}

    _, deps = _verified(
        tmp_path / "b", overrides={"quote": [_error(503, "SERVICE_UNAVAILABLE")] * 3}
    )
    down = quote_extension_impl(deps, "2027-06-17T14:00")
    assert down["outcome"] == "system_unavailable"
    assert deps.state.handoffs == {}
    assert _handoff_files(tmp_path / "a") == []
    assert _handoff_files(tmp_path / "b") == []


def test_requote_supersedes(tmp_path):
    _, deps = _verified(tmp_path)
    first = quote_extension_impl(deps, "2027-06-17T14:00")
    second = quote_extension_impl(deps, "2027-06-18T14:00")
    assert (first["quote_token"], second["quote_token"]) == ("q1", "q2")
    q1, q2 = deps.state.quotes
    assert q1.status == "superseded"
    assert q2.status == "presented"


def test_escalated_requote_voids_presented_quote(tmp_path):
    for i, (new_return, reason) in enumerate(
        [
            ("2027-06-30T14:00", "extension_too_long"),  # pre-quote guardrail
            ("2027-06-18T14:00", "high_value"),  # post-quote guardrail
        ]
    ):
        _, deps = _verified(tmp_path / str(i))
        assert quote_extension_impl(deps, "2027-06-17T14:00")["outcome"] == "quoted"
        deps.client.http.quote_charges["total_charged"] = 600.0
        result = quote_extension_impl(deps, new_return)
        assert result["outcome"] == "escalated"
        assert result["reason"] == reason
        assert deps.state.quotes[0].status == "voided"

    server, deps = _verified(tmp_path / "avail")
    assert quote_extension_impl(deps, "2027-06-17T14:00")["outcome"] == "quoted"
    server.availability = make_availability(available=False)
    result = quote_extension_impl(deps, "2027-06-18T14:00")
    assert result["reason"] == "availability_unconfirmed"
    assert deps.state.quotes[0].status == "voided"
