import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import requests
from fakes import FakeResponse, FakeRoadwayServer, make_deps, make_reservation

from agent import guarded
from payment import PaymentDetails, ScriptedPaymentCollector
from tools import (
    confirm_extension_impl,
    escalate_to_human_impl,
    quote_extension_impl,
    search_knowledge_base_impl,
    verify_customer_impl,
)

PDT = timezone(timedelta(hours=-7))
NOW = datetime(2027, 6, 14, 12, 0, tzinfo=PDT)
RID = "RWR-29471835"
EMAIL = "sarah.johnson@example.com"
NEW_RETURN = "2027-06-17T14:00"
SECRET_KEYS = {"cvv", "billing_zip", "payment"}


def _error(status, code, message="error"):
    return FakeResponse(
        status, {"success": False, "error": {"code": code, "message": message}}
    )


def _server(**kw):
    reservation = make_reservation()
    return FakeRoadwayServer({RID: reservation}, {RID: EMAIL}, **kw)


def _quoted(tmp_path, now=NOW, collector=None, email=EMAIL, **kw):
    server = _server(**kw)
    deps = make_deps(server, tmp_path, now, collector)
    assert verify_customer_impl(deps, RID, email)["outcome"] == "verified"
    assert quote_extension_impl(deps, NEW_RETURN)["quote_token"] == "q1"
    return server, deps


def _payment_calls(server):
    return [c for c in server.calls if c["endpoint"] == "extend_payment"]


def _keys(server):
    return [c["headers"]["Idempotency-Key"] for c in _payment_calls(server)]


def _handoffs(tmp_path):
    directory = tmp_path / "handoffs"
    if not directory.exists():
        return []
    return [json.loads(p.read_text(encoding="utf-8")) for p in directory.glob("*.json")]


def _session_records(tmp_path, deps):
    path = tmp_path / "sessions" / f"{deps.state.session_id}.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _leaks(value, secrets):
    """True if `value` holds a secret key or a string equal to a secret value."""
    if isinstance(value, dict):
        return any(
            (isinstance(k, str) and k.lower() in SECRET_KEYS) or _leaks(v, secrets)
            for k, v in value.items()
        )
    if isinstance(value, list):
        return any(_leaks(v, secrets) for v in value)
    return isinstance(value, str) and value in secrets


def _values(value):
    if isinstance(value, dict):
        for v in value.values():
            yield from _values(v)
    elif isinstance(value, list):
        for v in value:
            yield from _values(v)
    else:
        yield value


def test_confirm_happy(tmp_path):
    collector = ScriptedPaymentCollector([PaymentDetails("9731", "60614")])
    server, deps = _quoted(tmp_path, collector=collector)
    result = confirm_extension_impl(deps, "q1")
    assert result["outcome"] == "committed"
    assert result["confirmation_number"]
    assert len(server.commits) == 1
    assert _keys(server) == [f"ext-{deps.state.session_id}-q1-1"]
    assert not {"9731", "60614"} & {v for v in _values(result) if isinstance(v, str)}
    secrets = {"9731", "60614"}
    for record in _session_records(tmp_path, deps):
        assert not _leaks(record, secrets), record
    for handoff in _handoffs(tmp_path):
        assert not _leaks(handoff, secrets)


def test_confirm_refuses_without_token(tmp_path):
    collector = ScriptedPaymentCollector([PaymentDetails("847", "90210")])
    server, deps = _quoted(tmp_path, collector=collector)
    for token in ("", "q9"):
        assert confirm_extension_impl(deps, token)["outcome"] == "no_valid_quote"
    assert collector.calls == []
    assert _payment_calls(server) == []


def test_confirm_refuses_superseded(tmp_path):
    server, deps = _quoted(tmp_path)
    assert quote_extension_impl(deps, "2027-06-18T14:00")["quote_token"] == "q2"
    result = confirm_extension_impl(deps, "q1")
    assert result["outcome"] == "no_valid_quote"
    assert result["reason"] == "superseded"
    assert _payment_calls(server) == []


def test_confirm_refuses_stale(tmp_path):
    clock = {"now": NOW}
    server, deps = _quoted(tmp_path / "a", now=lambda: clock["now"])
    clock["now"] = NOW + timedelta(minutes=15)
    result = confirm_extension_impl(deps, "q1")
    assert result["outcome"] == "no_valid_quote"
    assert result["reason"] == "expired"
    assert _payment_calls(server) == []

    clock["now"] = NOW
    _, deps = _quoted(tmp_path / "b", now=lambda: clock["now"])
    clock["now"] = NOW + timedelta(minutes=14, seconds=59)
    assert confirm_extension_impl(deps, "q1")["outcome"] == "committed"


def test_confirm_after_decline(tmp_path):
    server, deps = _quoted(tmp_path, collector=ScriptedPaymentCollector([None]))
    assert confirm_extension_impl(deps, "q1")["outcome"] == "customer_declined"
    assert deps.state.quotes[0].status == "declined"
    second = confirm_extension_impl(deps, "q1")
    assert second["outcome"] == "no_valid_quote"
    assert second["reason"] == "not_presented"
    assert server.commits == []


def test_confirm_twice_charges_once(tmp_path):
    server, deps = _quoted(tmp_path)
    assert confirm_extension_impl(deps, "q1")["outcome"] == "committed"
    assert confirm_extension_impl(deps, "q1")["outcome"] == "no_valid_quote"
    assert len(_payment_calls(server)) == 1


def test_payment_failed_then_retry_new_key(tmp_path):
    server, deps = _quoted(
        tmp_path, overrides={"extend_payment": [_error(402, "PAYMENT_DECLINED")]}
    )
    first = confirm_extension_impl(deps, "q1")
    assert first["outcome"] == "payment_failed"
    assert first["attempts_remaining"] == 1
    assert confirm_extension_impl(deps, "q1")["outcome"] == "committed"
    sid = deps.state.session_id
    assert _keys(server) == [f"ext-{sid}-q1-1", f"ext-{sid}-q1-2"]


def test_payment_failed_twice_escalates(tmp_path):
    declined = _error(402, "PAYMENT_DECLINED")
    _, deps = _quoted(tmp_path, overrides={"extend_payment": [declined, declined]})
    assert confirm_extension_impl(deps, "q1")["outcome"] == "payment_failed"
    second = confirm_extension_impl(deps, "q1")
    assert second["outcome"] == "escalated"
    assert second["reason"] == "payment_failed"


def test_verify_at_commit_403(tmp_path):
    server, deps = _quoted(
        tmp_path,
        email="wrong@example.com",
        overrides={"extend_probe": [_error(400, "INVALID_EXTENSION")]},
    )
    assert deps.state.verification == "email_unconfirmed"
    result = confirm_extension_impl(deps, "q1")
    assert result["outcome"] == "not_verified"
    assert deps.state.quotes[0].status == "voided"
    assert deps.state.verification == "unverified"
    assert deps.state.email_failures == 1
    assert server.commits == []


def test_rejected_codes(tmp_path):
    cases = [
        _error(409, "RESERVATION_NOT_ACTIVE"),
        _error(400, "INVALID_EXTENSION"),
        _error(400, "INVALID_DATE"),
    ]
    for i, response in enumerate(cases):
        code = response.json()["error"]["code"]
        server, deps = _quoted(
            tmp_path / str(i), overrides={"extend_payment": [response]}
        )
        result = confirm_extension_impl(deps, "q1")
        assert result["outcome"] == "rejected"
        assert result["code"] == code
        assert server.commits == []


def test_unknown_outcome_hands_off_no_second_key(tmp_path):
    unavailable = _error(503, "SERVICE_UNAVAILABLE")
    server, deps = _quoted(tmp_path, overrides={"extend_payment": [unavailable] * 3})
    result = confirm_extension_impl(deps, "q1")
    assert result["outcome"] == "escalated"
    assert result["reason"] == "charge_status_unknown"
    key = f"ext-{deps.state.session_id}-q1-1"
    (handoff,) = _handoffs(tmp_path)
    assert handoff["idempotency_key"] == key
    assert handoff["charge_status"] == "unknown"
    assert _keys(server) == [key] * 3
    # Fix round 1 ruling: once a charge is unknown, confirm re-points to the
    # open handoff (was no_valid_quote in the brief); still no API call.
    again = confirm_extension_impl(deps, "q1")
    assert again["outcome"] == "escalated"
    assert again["already_open"] is True
    assert len(_payment_calls(server)) == 3


def test_amount_differs_flagged(tmp_path):
    _, deps = _quoted(tmp_path, extend_charges={"total_charged": 101.00})
    result = confirm_extension_impl(deps, "q1")
    assert result["outcome"] == "committed"
    assert result["amount_differs_from_quote"] is True
    assert any(e["level"] == "warning" for e in deps.state.trace)


def test_escalate_dedup(tmp_path):
    deps = make_deps(_server(), tmp_path, NOW)
    first = escalate_to_human_impl(deps, "cancel_request", "Customer wants to cancel.")
    second = escalate_to_human_impl(deps, "cancel_request", "Customer wants to cancel.")
    assert first["outcome"] == second["outcome"] == "escalated"
    assert first["already_open"] is False
    assert second["already_open"] is True
    assert second["handoff_reference"] == first["handoff_reference"]


def test_escalate_without_verification(tmp_path):
    deps = make_deps(_server(), tmp_path, NOW)
    result = escalate_to_human_impl(deps, "cancel_request", "Customer wants to cancel.")
    assert result["outcome"] == "escalated"
    (handoff,) = _handoffs(tmp_path)
    assert handoff["verified"] is False


def test_search_kb_tool(tmp_path):
    deps = make_deps(_server(), tmp_path, NOW)
    result = search_knowledge_base_impl(deps, "grace period for late returns")
    assert result["outcome"] == "results"
    assert 0 < len(result["articles"]) <= 3
    assert any(e["event"] == "kb_search" for e in deps.state.trace)


def _unknown_q1(tmp_path, collector=None):
    unavailable = _error(503, "SERVICE_UNAVAILABLE")
    server, deps = _quoted(
        tmp_path, collector=collector, overrides={"extend_payment": [unavailable] * 3}
    )
    first = confirm_extension_impl(deps, "q1")
    assert first["reason"] == "charge_status_unknown"
    return server, deps, first


def test_quote_after_unknown_charge_escalates(tmp_path):
    server, deps, first = _unknown_q1(tmp_path)
    calls_before = len(server.calls)
    result = quote_extension_impl(deps, "2027-06-18T14:00")
    assert result["outcome"] == "escalated"
    assert result["reason"] == "charge_status_unknown"
    assert result["handoff_reference"] == first["handoff_reference"]
    assert result["already_open"] is True
    assert result["message"] == first["message"]
    assert len(server.calls) == calls_before
    assert len(deps.state.quotes) == 1


def test_confirm_new_quote_after_unknown_charge_escalates(tmp_path):
    collector = ScriptedPaymentCollector([PaymentDetails("847", "90210")] * 3)
    server, deps, first = _unknown_q1(tmp_path, collector)
    q1 = deps.state.quotes[0]
    deps.state.add_quote(replace(q1, token="q2", status="presented", attempts=0))
    calls_before, collected_before = len(server.calls), len(collector.calls)
    result = confirm_extension_impl(deps, "q2")
    assert result["outcome"] == "escalated"
    assert result["reason"] == "charge_status_unknown"
    assert result["handoff_reference"] == first["handoff_reference"]
    assert result["already_open"] is True
    assert result["message"] == first["message"]
    assert len(server.calls) == calls_before
    assert len(collector.calls) == collected_before


def test_unexpected_request_exception_is_unknown(tmp_path):
    chunked = requests.exceptions.ChunkedEncodingError("broken body")
    server, deps = _quoted(tmp_path, overrides={"extend_payment": [chunked] * 3})
    result = confirm_extension_impl(deps, "q1")
    assert result["outcome"] == "escalated"
    assert result["reason"] == "charge_status_unknown"
    assert deps.state.quotes[0].status == "unknown"
    assert len(_keys(server)) == 3
    assert len(set(_keys(server))) == 1


# -- final review fixes --------------------------------------------------------


def _break_log_on_first_charge(deps):
    """on_attempt raises once the charge response has arrived (log write fails);
    the reproduction from the final whole-branch review."""
    original = deps.client.on_attempt

    def flaky(attempt):
        key = attempt.get("idempotency_key") or ""
        if key.startswith("ext-") and key.endswith("-1"):
            raise OSError("log file locked")
        original(attempt)

    deps.client.on_attempt = flaky


def test_log_failure_after_charge_does_not_charge_twice(tmp_path):
    collector = ScriptedPaymentCollector([PaymentDetails("847", "90210")] * 2)
    server, deps = _quoted(tmp_path, collector=collector)
    _break_log_on_first_charge(deps)
    first = guarded(
        deps, "confirm_extension", lambda: confirm_extension_impl(deps, "q1")
    )
    assert first["outcome"] == "committed"
    second = guarded(
        deps, "confirm_extension", lambda: confirm_extension_impl(deps, "q1")
    )
    assert second["outcome"] == "no_valid_quote"
    assert len(server.commits) == 1
    assert _keys(server) == [f"ext-{deps.state.session_id}-q1-1"]


def test_exception_during_charge_is_unknown_and_never_recharged(tmp_path):
    collector = ScriptedPaymentCollector([PaymentDetails("847", "90210")] * 2)
    server, deps = _quoted(tmp_path, collector=collector)

    def extend(*args, **kwargs):
        raise RuntimeError("bug after the request was sent")

    deps.client.extend = extend
    first = guarded(
        deps, "confirm_extension", lambda: confirm_extension_impl(deps, "q1")
    )
    assert first["outcome"] == "escalated"
    assert first["reason"] == "charge_status_unknown"
    assert deps.state.quotes[0].status == "unknown"
    key = f"ext-{deps.state.session_id}-q1-1"
    (handoff,) = _handoffs(tmp_path)
    assert handoff["idempotency_key"] == key
    assert handoff["charge_status"] == "unknown"
    second = guarded(
        deps, "confirm_extension", lambda: confirm_extension_impl(deps, "q1")
    )
    assert second["outcome"] == "escalated"
    assert second["already_open"] is True
    assert server.commits == []
    assert len(collector.calls) == 1


def test_quote_is_blocking_while_charge_in_flight(tmp_path):
    seen = {}
    _, deps = _quoted(tmp_path)
    original = deps.client.extend

    def extend(*args, **kwargs):
        seen["status"] = deps.state.quotes[0].status
        seen["quote"] = quote_extension_impl(deps, "2027-06-18T14:00")
        return original(*args, **kwargs)

    deps.client.extend = extend
    assert confirm_extension_impl(deps, "q1")["outcome"] == "committed"
    assert seen["status"] == "in_flight"
    assert seen["quote"]["outcome"] == "escalated"
    assert seen["quote"]["reason"] == "charge_status_unknown"
    assert deps.state.quotes[0].status == "committed"


def test_confirm_committed_quote_reports_already_committed(tmp_path):
    server, deps = _quoted(tmp_path)
    first = confirm_extension_impl(deps, "q1")
    calls_before = len(server.calls)
    again = confirm_extension_impl(deps, "q1")
    assert again["outcome"] == "no_valid_quote"
    assert again["reason"] == "already_committed"
    assert again["confirmation_number"] == first["confirmation_number"]
    assert again["new_return_local"] == first["new_return_local"]
    assert again["total_charged"] == first["charges"]["total_charged"]
    assert len(server.calls) == calls_before


def test_commit_updates_current_return_for_next_extension(tmp_path):
    _, deps = _quoted(tmp_path)
    assert confirm_extension_impl(deps, "q1")["outcome"] == "committed"
    current = deps.state.reservation["dates"]["current_return_datetime"]
    assert current.startswith("2027-06-17T14:00")
    # The committed return is now the floor for a second extension.
    result = quote_extension_impl(deps, "2027-06-16T14:00")
    assert result["outcome"] == "invalid_request"
    assert "2027-06-17T14:00" in result["message"]


def test_ttl_rechecked_after_payment_prompt(tmp_path):
    clock = {"now": NOW}

    class SlowCollector:
        def collect(self, *args, **kwargs):
            clock["now"] = NOW + timedelta(minutes=16)
            return PaymentDetails("847", "90210")

    server, deps = _quoted(
        tmp_path, now=lambda: clock["now"], collector=SlowCollector()
    )
    result = confirm_extension_impl(deps, "q1")
    assert result["outcome"] == "no_valid_quote"
    assert result["reason"] == "expired"
    assert _payment_calls(server) == []


def test_payment_failed_retry_keeps_quote_presented(tmp_path):
    _, deps = _quoted(
        tmp_path, overrides={"extend_payment": [_error(402, "PAYMENT_DECLINED")]}
    )
    assert confirm_extension_impl(deps, "q1")["outcome"] == "payment_failed"
    assert deps.state.quotes[0].status == "presented"


def test_email_unconfirmed_quote_and_prompt_withhold_card(tmp_path):
    collector = ScriptedPaymentCollector([PaymentDetails("847", "90210")])
    server = _server(overrides={"extend_probe": [_error(400, "INVALID_EXTENSION")]})
    deps = make_deps(server, tmp_path, NOW, collector)
    assert verify_customer_impl(deps, RID, EMAIL)["email_confirmed"] is False
    quoted = quote_extension_impl(deps, NEW_RETURN)
    assert quoted["outcome"] == "quoted"
    assert quoted["card_type"] is None and quoted["card_last_four"] is None
    result = confirm_extension_impl(deps, "q1")
    assert result["outcome"] == "committed"
    assert collector.calls[0][1:] == (None, None)
