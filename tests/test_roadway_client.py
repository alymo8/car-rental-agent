import random

import pytest
import requests

from config import Settings
from roadway_client import RoadwayAPIError, RoadwayClient
from tests.fakes import FakeResponse, ScriptedHTTP

OK = {"reservation_id": "RWR-1"}
EMAIL = "david.kim@example.com"
PAYMENT = {"card_number": "4111111111111111", "cvv": "847", "billing_zip": "90210"}
NEW_RETURN = "2027-06-17T14:00:00-07:00"


def make(script, attempts=None):
    http = ScriptedHTTP(script)
    sleeps: list[float] = []
    client = RoadwayClient(
        "https://api.test",
        "key",
        Settings(),
        http=http,
        on_attempt=attempts.append if attempts is not None else None,
        sleep=sleeps.append,
        rng=random.Random(0),
    )
    return client, http, sleeps


def envelope(code, message="m", details=None):
    return {"error": {"code": code, "message": message, "details": details or {}}}


def test_retries_5xx_then_succeeds():
    client, http, sleeps = make(
        [
            FakeResponse(503, envelope("X")),
            FakeResponse(503, "x"),
            FakeResponse(200, OK),
        ]
    )
    assert client.get_reservation("RWR-1") == OK
    assert len(http.calls) == 3
    assert len(sleeps) == 2
    assert 0.4 <= sleeps[0] <= 0.6
    assert 0.8 <= sleeps[1] <= 1.2


def test_retries_timeout():
    client, http, _ = make([requests.Timeout("slow"), FakeResponse(200, OK)])
    assert client.get_reservation("RWR-1") == OK
    assert len(http.calls) == 2


def test_no_retry_on_4xx():
    client, http, _ = make([FakeResponse(404, envelope("RESERVATION_NOT_FOUND"))])
    with pytest.raises(RoadwayAPIError) as ei:
        client.get_reservation("RWR-0")
    assert ei.value.status == 404
    assert ei.value.code == "RESERVATION_NOT_FOUND"
    assert ei.value.retryable is False
    assert len(http.calls) == 1


def test_gives_up_after_three():
    client, http, _ = make([FakeResponse(500, envelope("INTERNAL"))] * 3)
    with pytest.raises(RoadwayAPIError) as ei:
        client.get_reservation("RWR-1")
    assert ei.value.status == 500
    assert ei.value.retryable is True
    assert len(http.calls) == 3


def test_transport_error_after_retries():
    client, http, _ = make([requests.ConnectionError("down")] * 3)
    with pytest.raises(RoadwayAPIError) as ei:
        client.get_reservation("RWR-1")
    assert ei.value.status is None
    assert ei.value.code == "TRANSPORT_ERROR"
    assert ei.value.retryable is True
    assert len(http.calls) == 3


def test_write_without_key_not_retried():
    client, http, _ = make([FakeResponse(503, envelope("X")), FakeResponse(200, OK)])
    with pytest.raises(RoadwayAPIError):
        client.extend(
            "RWR-1",
            email=EMAIL,
            new_return_datetime=NEW_RETURN,
            payment=None,
            idempotency_key=None,
        )
    assert len(http.calls) == 1


def test_write_with_key_retried_with_same_key():
    client, http, _ = make([FakeResponse(503, envelope("X")), FakeResponse(200, OK)])
    out = client.extend(
        "RWR-1",
        email=EMAIL,
        new_return_datetime=NEW_RETURN,
        payment=PAYMENT,
        idempotency_key="k1",
    )
    assert out == OK
    assert len(http.calls) == 2
    assert all(c["headers"]["Idempotency-Key"] == "k1" for c in http.calls)


def test_quote_retried_without_key():
    client, http, _ = make([FakeResponse(503, envelope("X")), FakeResponse(200, OK)])
    assert client.quote_extension("RWR-1", NEW_RETURN) == OK
    assert len(http.calls) == 2
    assert all("Idempotency-Key" not in c["headers"] for c in http.calls)


def test_availability_uses_longer_read_timeout():
    client, http, _ = make([FakeResponse(200, {"available": True})])
    client.get_availability("LAX", "midsize", NEW_RETURN, "2027-06-18T14:00:00-07:00")
    assert http.calls[0]["timeout"] == (3.0, 12.0)


def test_error_fields():
    details = {"reason": "insufficient_funds"}
    client, _, _ = make(
        [FakeResponse(402, envelope("PAYMENT_DECLINED", "Card declined", details))]
    )
    with pytest.raises(RoadwayAPIError) as ei:
        client.extend(
            "RWR-1",
            email=EMAIL,
            new_return_datetime=NEW_RETURN,
            payment=PAYMENT,
            idempotency_key="k1",
        )
    assert ei.value.status == 402
    assert ei.value.code == "PAYMENT_DECLINED"
    assert ei.value.message == "Card declined"
    assert ei.value.details == details


def test_non_json_error_body():
    client, _http, _ = make([FakeResponse(502, "<html>")] * 3)
    with pytest.raises(RoadwayAPIError) as ei:
        client.get_reservation("RWR-1")
    assert ei.value.status == 502
    assert ei.value.code == "HTTP_502"
    assert ei.value.retryable is True


def test_on_attempt_never_sees_email_or_payment():
    attempts: list[dict] = []
    client, _, _ = make(
        [FakeResponse(503, envelope("X")), FakeResponse(200, OK)], attempts=attempts
    )
    client.extend(
        "RWR-1",
        email=EMAIL,
        new_return_datetime=NEW_RETURN,
        payment=PAYMENT,
        idempotency_key="k1",
    )
    assert len(attempts) == 2
    for a in attempts:
        text = str(a)
        assert "email" not in text
        assert EMAIL not in text
        assert "cvv" not in text
        assert "847" not in text


def test_other_request_exceptions_are_retryable_transport_errors():
    chunked = requests.exceptions.ChunkedEncodingError("broken body")
    client, http, _ = make([chunked, chunked, chunked])
    with pytest.raises(RoadwayAPIError) as ei:
        client.extend(
            "RWR-1",
            email=EMAIL,
            new_return_datetime=NEW_RETURN,
            payment=PAYMENT,
            idempotency_key="k-1",
        )
    assert ei.value.status is None
    assert ei.value.code == "TRANSPORT_ERROR"
    assert ei.value.retryable is True
    assert len(http.calls) == 3
    assert {c["headers"]["Idempotency-Key"] for c in http.calls} == {"k-1"}


def test_on_attempt_failure_does_not_change_outcome():
    def broken_log(_attempt):
        raise OSError("log file locked")

    http = ScriptedHTTP([FakeResponse(503, envelope("X")), FakeResponse(200, OK)])
    client = RoadwayClient(
        "https://api.test",
        "key",
        Settings(),
        http=http,
        on_attempt=broken_log,
        sleep=lambda s: None,
        rng=random.Random(0),
    )
    assert client.get_reservation("RWR-1") == OK
    assert len(http.calls) == 2
