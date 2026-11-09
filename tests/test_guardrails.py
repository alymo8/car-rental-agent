from datetime import datetime, timedelta

from config import load_settings
from guardrails import ALLOW, Verdict, availability_verdict, post_quote, pre_quote

CUR = datetime.fromisoformat("2027-06-15T14:00-07:00")
NOW = datetime.fromisoformat("2027-06-14T12:00-07:00")
S = load_settings({})


def test_overdue_boundary():
    assert (
        pre_quote(CUR, CUR + timedelta(days=2), CUR + timedelta(minutes=30), S) == ALLOW
    )
    assert pre_quote(
        CUR, CUR + timedelta(days=2), CUR + timedelta(minutes=31), S
    ) == Verdict("escalate", "overdue_rental")


def test_not_later_is_invalid():
    expected = Verdict("invalid", "not_later_than_current_return")
    assert pre_quote(CUR, CUR, NOW, S) == expected
    assert pre_quote(CUR, CUR - timedelta(hours=1), NOW, S) == expected


def test_short_extension_boundary():
    assert pre_quote(CUR, CUR + timedelta(minutes=30), NOW, S) == Verdict(
        "advise_not_needed"
    )
    assert pre_quote(CUR, CUR + timedelta(minutes=31), NOW, S) == ALLOW
    assert (
        pre_quote(CUR, CUR + timedelta(minutes=30), NOW, S, customer_insists=True)
        == ALLOW
    )


def test_max_days_boundary():
    assert pre_quote(CUR, CUR + timedelta(days=14), NOW, S) == ALLOW
    assert pre_quote(CUR, CUR + timedelta(days=14, minutes=1), NOW, S) == Verdict(
        "escalate", "extension_too_long"
    )


def test_post_quote_boundaries():
    assert post_quote({"extension_days": 2, "total_charged": 500.00}, S) == ALLOW
    assert post_quote({"extension_days": 2, "total_charged": 500.01}, S) == Verdict(
        "escalate", "high_value"
    )
    assert post_quote({"extension_days": 15, "total_charged": 100.00}, S) == Verdict(
        "escalate", "extension_too_long"
    )
    assert post_quote({"extension_days": 14, "total_charged": 100.00}, S) == ALLOW


def test_availability():
    unconfirmed = Verdict("escalate", "availability_unconfirmed")
    assert availability_verdict(None) == unconfirmed
    unavailable = {"availability": {"requested_type": {"available": False}}}
    assert availability_verdict(unavailable) == unconfirmed
    available = {"availability": {"requested_type": {"available": True}}}
    assert availability_verdict(available) == ALLOW


def test_overdue_checked_first():
    now = CUR + timedelta(hours=1)
    assert pre_quote(CUR, CUR - timedelta(hours=1), now, S) == Verdict(
        "escalate", "overdue_rental"
    )
