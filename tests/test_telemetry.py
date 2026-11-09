import json

import pytest

from telemetry import SessionLogger, mask_email, redact


def test_mask_email():
    assert mask_email("david.kim@example.com") == "d***@example.com"


def test_redact_drops_payment_keys_at_any_depth():
    out = redact(
        {"a": {"payment": {"cvv": "847"}, "cvv": "1", "billing_zip": "90210", "ok": 1}}
    )
    assert out == {"a": {"ok": 1}}


def test_redact_masks_emails_in_free_text():
    assert (
        redact({"text": "I'm Sarah.Johnson@example.com"})["text"]
        == "I'm S***@example.com"
    )


def test_logger_writes_redacted_jsonl(tmp_path):
    log = SessionLogger("abc", tmp_path)
    log.write("tool_call", args={"email": "x.y@example.com", "cvv": "9731"})
    rec = json.loads(log.path.read_text().strip())
    assert rec["event"] == "tool_call" and rec["args"] == {"email": "x***@example.com"}


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("my card is 4111111111111111", "my card is [card]"),
        ("card 4111 1111 1111 1111 ok", "card [card] ok"),
        ("card 4111-1111-1111-1111.", "card [card]."),
        ("amex 3782 822463 10005", "amex [card]"),
        ("long 6011 0009 9013 9424 123 end", "long [card] end"),
        ("CVV: 847", "CVV: [cvv]"),
        ("my cvv is 9731", "my cvv is [cvv]"),
        ("cvc 123, cvv2=456", "cvc [cvv], cvv2=[cvv]"),
        ("Security code 847", "Security code [cvv]"),
        ("zip 90210", "zip [zip]"),
        ("ZIP code: 60614-1234", "ZIP code: [zip]"),
        ("billing zip is 90210", "billing zip is [zip]"),
        ("postal code 10001", "postal code [zip]"),
        (
            "cvv 847 zip 90210 card 4111111111111111",
            "cvv [cvv] zip [zip] card [card]",
        ),
    ],
)
def test_redact_scrubs_card_data_in_free_text(text, expected):
    assert redact({"text": text})["text"] == expected


@pytest.mark.parametrize(
    "text",
    [
        "Reservation RWR-29471835 for USD 100.49",
        "Extend to 2027-06-17T14:00 from 2027-06-15T14:00:00-07:00",
        "between 2027-06-17 2027-06-18 at $1,234.56",
        "ext-1a2b3c4d-q1-2 and HO-1a2b3c4d-1 and EXT-000001",
        "call 555-123-4567, 3 days, 14 days, 500 dollars",
        "the 90210 area code 847 is fine",
    ],
)
def test_redact_leaves_ids_amounts_and_dates(text):
    assert redact({"text": text})["text"] == text


def test_unknown_event_rejected(tmp_path):
    with pytest.raises(ValueError):
        SessionLogger("abc", tmp_path).write("nope")
