"""Scripted eval scenarios and the trace checks that grade them.

Each scenario is a fixed list of customer messages plus scripted payment
responses. Checks look at the session trace (tool outcomes, handoff reasons,
committed extensions), not at wording, except `grace_question`, which also
checks the answer text.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from payment import PaymentDetails
from timeutil import format_local, parse_api_datetime, zone_for

PASS = "pass"
FAIL = "fail"
SKIPPED = "skipped (precondition)"

Check = Callable[[list[dict], str], tuple[str, str]]


def _default_payments() -> list[PaymentDetails | None]:
    return [PaymentDetails("847", "90210")]


@dataclass
class Scenario:
    name: str
    reservation_id: str | None
    email: str | None
    messages: list[str]  # templates with {rid}, {email}, {new_local}
    check: Check
    extend_by: timedelta | None = None
    payments: list[PaymentDetails | None] = field(default_factory=_default_payments)


# -- trace helpers ---------------------------------------------------------------


def tool_results(trace: list[dict], name: str) -> list[dict]:
    """The `result` dicts of every `tool_result` event for tool `name`, in order."""
    return [
        e.get("result") or {}
        for e in trace
        if e.get("event") == "tool_result" and e.get("tool") == name
    ]


def commits(trace: list[dict]) -> int:
    """Number of `confirm_extension` results with outcome `committed`."""
    return sum(
        1
        for r in tool_results(trace, "confirm_extension")
        if r.get("outcome") == "committed"
    )


def handoff_reasons(trace: list[dict]) -> list[str]:
    return [e.get("reason") for e in trace if e.get("event") == "handoff"]


def kb_result_ids(trace: list[dict]) -> list[list[str]]:
    """Article IDs of each `kb_search` event, in rank order."""
    return [
        [r.get("id") for r in e.get("results") or []]
        for e in trace
        if e.get("event") == "kb_search"
    ]


def new_return_local(reservation: dict, now: datetime, delta: timedelta) -> str:
    """`YYYY-MM-DDTHH:MM` local time at the return location of
    max(current return, now) + delta, rounded down to the minute."""
    zone = zone_for(
        reservation["return_location"]["code"],
        reservation["dates"]["pickup_datetime"],
    )
    current = parse_api_datetime(reservation["dates"]["current_return_datetime"])
    return format_local(max(current, now) + delta, zone)


# -- check building blocks -------------------------------------------------------


def _grade(conditions: list[tuple[bool, str]]) -> tuple[str, str]:
    """`pass` when every condition holds; otherwise `fail` naming those that did not."""
    failed = [label for ok, label in conditions if not ok]
    if failed:
        return FAIL, "failed: " + "; ".join(failed)
    return PASS, "; ".join(label for _, label in conditions)


def _outcomes(trace: list[dict], tool: str) -> list[str]:
    return [r.get("outcome") for r in tool_results(trace, tool)]


def _verified(trace: list[dict]) -> tuple[bool, str]:
    return "verified" in _outcomes(trace, "verify_customer"), "verified"


def _commits_eq(trace: list[dict], n: int) -> tuple[bool, str]:
    got = commits(trace)
    return got == n, f"commits == {n} (got {got})"


def _handoff(trace: list[dict], reason: str) -> tuple[bool, str]:
    reasons = handoff_reasons(trace)
    return reason in reasons, f"handoff {reason} (got {reasons})"


def _kb_searched(trace: list[dict]) -> tuple[bool, str]:
    return bool(kb_result_ids(trace)), "kb_search event"


def _happy(trace: list[dict], replies: str) -> tuple[str, str]:
    return _grade([_verified(trace), _commits_eq(trace, 1)])


def _preferred_happy(trace: list[dict], replies: str) -> tuple[str, str]:
    quoted = [
        r
        for r in tool_results(trace, "quote_extension")
        if r.get("outcome") == "quoted"
    ]
    exact = any(
        r.get("total_charged") == 100.49 and r.get("late_fee") == 0 for r in quoted
    )
    seen = [(r.get("total_charged"), r.get("late_fee")) for r in quoted]
    return _grade(
        [
            _verified(trace),
            (exact, f"quoted total 100.49, late fee 0 (got {seen})"),
            _commits_eq(trace, 1),
        ]
    )


def _overdue(trace: list[dict], replies: str) -> tuple[str, str]:
    return _grade([_handoff(trace, "overdue_rental"), _commits_eq(trace, 0)])


def _availability_timeout(trace: list[dict], replies: str) -> tuple[str, str]:
    status, detail = _grade(
        [_handoff(trace, "availability_unconfirmed"), _commits_eq(trace, 0)]
    )
    if status == PASS:
        return status, detail
    verdicts = [
        e.get("verdict")
        for e in trace
        if e.get("event") == "guardrail" and e.get("check") == "availability"
    ]
    if verdicts and verdicts[-1] == "allow":
        return SKIPPED, "the mock answered availability and the vehicle was available"
    return status, detail


def _declines_quote(trace: list[dict], replies: str) -> tuple[str, str]:
    quoted = "quoted" in _outcomes(trace, "quote_extension")
    return _grade([(quoted, "quote presented"), _commits_eq(trace, 0)])


def _wrong_email_twice(trace: list[dict], replies: str) -> tuple[str, str]:
    return _grade([_handoff(trace, "verification_failed"), _commits_eq(trace, 0)])


_THIRTY_MINUTES = re.compile(r"30[- ]minute", re.IGNORECASE)


def _first_kb_results(trace: list[dict]) -> list[dict]:
    for e in trace:
        if e.get("event") == "kb_search":
            return e.get("results") or []
    return []


def _grace_question(trace: list[dict], replies: str) -> tuple[str, str]:
    # Official policy must outrank the legacy "2 hours" article; which official
    # article ranks first does not matter.
    first = _first_kb_results(trace)
    ids = [r.get("id") for r in first]
    ranked = bool(first) and first[0].get("authority") == "official-policy"
    return _grade(
        [
            _kb_searched(trace),
            (
                ranked,
                f"top result official-policy, above kb_fee_01 (first search: {ids})",
            ),
            (bool(_THIRTY_MINUTES.search(replies)), "reply matches 30[- ]minute"),
            ("2 hours" not in replies, "reply has no '2 hours'"),
            _commits_eq(trace, 0),
        ]
    )


def _cancel_request(trace: list[dict], replies: str) -> tuple[str, str]:
    escalated = bool(tool_results(trace, "escalate_to_human"))
    return _grade(
        [
            _kb_searched(trace),
            (escalated, "escalate_to_human called"),
            _commits_eq(trace, 0),
        ]
    )


def _tiny_extension(trace: list[dict], replies: str) -> tuple[str, str]:
    advised = "advise_not_needed" in _outcomes(trace, "quote_extension")
    return _grade([(advised, "advise_not_needed"), _commits_eq(trace, 0)])


# -- scenarios -------------------------------------------------------------------

_ASK = "I'd like to extend my rental."
_IDENTIFY = "{rid}, {email}"
_NEW_TIME = "Please change the return to {new_local}."
_YES = "Yes, go ahead."
_EXTEND = [_ASK, _IDENTIFY, _NEW_TIME, _YES]
_TWO_DAYS = timedelta(days=2)

SCENARIOS: list[Scenario] = [
    Scenario(
        "preferred_happy",
        "RWR-29471835",
        "sarah.johnson@example.com",
        list(_EXTEND),
        _preferred_happy,
        extend_by=_TWO_DAYS,
    ),
    Scenario(
        "standard_happy",
        "RWR-66002030",
        "david.kim@example.com",
        list(_EXTEND),
        _happy,
        extend_by=_TWO_DAYS,
    ),
    Scenario(
        "due_soon",
        "RWR-77001020",
        "priya.patel@example.com",
        list(_EXTEND),
        _happy,
        extend_by=_TWO_DAYS,
    ),
    Scenario(
        "overdue",
        "RWR-48372915",
        "marcus.lee@example.com",
        [_ASK, _IDENTIFY, _NEW_TIME],
        _overdue,
        extend_by=_TWO_DAYS,
    ),
    Scenario(
        "availability_timeout",
        "RWR-99004050",
        "tomas.rivera@example.com",
        list(_EXTEND),
        _availability_timeout,
        extend_by=_TWO_DAYS,
    ),
    Scenario(
        "declines_quote",
        "RWR-50000001",
        "robert.chen@example.com",
        [_ASK, _IDENTIFY, _NEW_TIME, "No, I don't want to go ahead."],
        _declines_quote,
        extend_by=_TWO_DAYS,
        payments=[None],
    ),
    Scenario(
        "wrong_email_twice",
        "RWR-66002030",
        None,
        [
            "I want to extend {rid}. My email is wrong.one@example.com",
            "Sorry, try wrong.two@example.com",
        ],
        _wrong_email_twice,
    ),
    Scenario(
        "grace_question",
        None,
        None,
        ["How long is the grace period if I return my car late?"],
        _grace_question,
    ),
    Scenario(
        "cancel_request",
        "RWR-29471835",
        None,
        ["I need to cancel reservation {rid}. What is your cancellation policy?"],
        _cancel_request,
    ),
    Scenario(
        "tiny_extension",
        "RWR-66002030",
        "david.kim@example.com",
        [_ASK, _IDENTIFY, _NEW_TIME, "OK, never mind then."],
        _tiny_extension,
        extend_by=timedelta(minutes=20),
    ),
]
