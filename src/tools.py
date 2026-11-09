"""Agent tools. Each `*_impl(deps, ...)` is a plain, testable function holding the
money and identity rules; the SDK wrappers (Task 11) only read `ToolDeps` from
the run context and call them.

Every result is a short dict with an `outcome` the prompt keys off.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from config import Settings
from guardrails import Verdict, availability_verdict, post_quote, pre_quote
from handoff import HandoffWriter
from kb import KnowledgeBase
from payment import PaymentCollector
from roadway_client import RoadwayAPIError, RoadwayClient
from session import CHARGE_PENDING_STATUSES, ConversationState, Quote
from timeutil import (
    InvalidLocalTime,
    format_local,
    parse_api_datetime,
    parse_local,
    to_api,
    zone_for,
    zone_label,
)

ADVISE_NOT_NEEDED_MESSAGE = (
    "Returns within 30 minutes of the scheduled time are covered by the grace "
    "period, and an extension bills a full day. If the customer still wants it, "
    "call again with customer_insists=true."
)
NOT_VERIFIED_AT_COMMIT_MESSAGE = (
    "The email did not match. Verify again with verify_customer, then get a new quote."
)
CHARGE_UNKNOWN_MESSAGE = (
    "We could not confirm whether the extension went through. It will not be "
    "retried; a representative will check and follow up."
)
PAYMENT_ERROR_CODES = ("PAYMENT_DECLINED", "PAYMENT_VALIDATION_ERROR")


@dataclass
class ToolDeps:
    state: ConversationState
    client: RoadwayClient
    payments: PaymentCollector
    kb: KnowledgeBase
    handoffs: HandoffWriter
    settings: Settings
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)


# -- shared helpers ------------------------------------------------------------


def _traced(deps: ToolDeps, tool: str, args: dict, run: Callable[[], dict]) -> dict:
    deps.state.record("tool_call", tool=tool, args=args)
    result = run()
    deps.state.record("tool_result", tool=tool, result=result)
    return result


def _guardrail(deps: ToolDeps, check: str, verdict: Verdict) -> Verdict:
    deps.state.record(
        "guardrail", check=check, verdict=verdict.kind, reason=verdict.reason
    )
    return verdict


def _escalate(
    deps: ToolDeps,
    reason: str,
    summary: str,
    *,
    message: str | None = None,
    idempotency_key: str | None = None,
    charge_status: str | None = None,
) -> dict:
    handoff = deps.handoffs.open(
        deps.state,
        reason,
        summary,
        idempotency_key=idempotency_key,
        charge_status=charge_status,
        now=deps.now(),
    )
    reference = handoff["handoff_reference"]
    return {
        "outcome": "escalated",
        "reason": reason,
        "handoff_reference": reference,
        "already_open": handoff["already_open"],
        "message": message
        or f"A representative will take this over; the reference is {reference}.",
    }


def _charge_unknown_block(deps: ToolDeps) -> dict | None:
    """Once a charge is in flight or its outcome is unknown, no further quote or
    charge in this session: a new quote would mean a new key and a possible
    second charge. Returns the `charge_status_unknown` handoff (deduplicated)."""
    pending = [q for q in deps.state.quotes if q.status in CHARGE_PENDING_STATUSES]
    if not pending:
        return None
    return _escalate(
        deps,
        "charge_status_unknown",
        "Further extension requested after a charge with unknown outcome.",
        message=CHARGE_UNKNOWN_MESSAGE,
        idempotency_key=pending[-1].charge_key,
        charge_status="unknown",
    )


def _void_presented(state: ConversationState) -> None:
    for quote in state.quotes:
        if quote.status == "presented":
            quote.status = "voided"


def _card_if_confirmed(state: ConversationState) -> dict:
    """The card on file, or {} while the email is unconfirmed: nothing about the
    booking is disclosed before the email is confirmed."""
    if state.verification != "verified":
        return {}
    return (state.reservation or {}).get("payment", {}).get("card_on_file", {})


def _is_client_error(err: RoadwayAPIError) -> bool:
    return err.status is not None and 400 <= err.status < 500


# -- verify_customer -----------------------------------------------------------


def verify_customer_impl(deps: ToolDeps, reservation_id: str, email: str) -> dict:
    return _traced(
        deps,
        "verify_customer",
        {"reservation_id": reservation_id, "email": email},
        lambda: _verify(deps, reservation_id, email),
    )


def _wrong_email(deps: ToolDeps) -> dict:
    state, limit = deps.state, deps.settings.max_email_failures
    state.email_failures += 1
    if state.email_failures >= limit:
        return _escalate(
            deps,
            "verification_failed",
            f"Customer could not be verified for {state.reservation_id} after "
            f"{state.email_failures} attempts.",
        )
    return {
        "outcome": "not_verified",
        "attempts_remaining": limit - state.email_failures,
    }


def _verify(deps: ToolDeps, reservation_id: str, email: str) -> dict:
    state = deps.state
    rid = reservation_id.strip().upper()
    email = email.strip().lower()
    state.verify_attempts += 1
    # A new verification attempt drops any earlier one, so a failed attempt on
    # another booking can never leave the session verified for the wrong ID.
    state.verification = "unverified"
    state.reservation = state.email = state.zone = None
    _void_presented(state)
    state.reservation_id = rid

    try:
        reservation = deps.client.get_reservation(rid)
    except RoadwayAPIError as err:
        if err.status == 404:
            return _wrong_email(deps)
        return {"outcome": "system_unavailable"}

    dates = reservation["dates"]
    zone = zone_for(reservation["return_location"]["code"], dates["pickup_datetime"])
    current_return = parse_api_datetime(dates["current_return_datetime"])
    now = deps.now()
    probe_return = max(current_return, now) + timedelta(days=1)
    active = reservation.get("status") == "active"

    try:
        deps.client.extend(
            rid,
            email=email,
            new_return_datetime=to_api(probe_return, zone),
            payment=None,
            idempotency_key=f"verify-{state.session_id}-{state.verify_attempts}",
        )
    except RoadwayAPIError as err:
        if err.status == 403:
            return _wrong_email(deps)
        if err.status is None or err.status >= 500:
            # Checked before the status rule: without the email check we may
            # not disclose whether the booking is active.
            return {"outcome": "system_unavailable"}
        if err.code == "RESERVATION_NOT_ACTIVE" or not active:
            return {"outcome": "not_eligible"}
        if err.code == "PAYMENT_VALIDATION_ERROR":
            verification = "verified"
        elif _is_client_error(err):
            verification = "email_unconfirmed"
        else:  # a non-2xx outside 4xx/5xx (e.g. 3xx): nothing usable
            return {"outcome": "system_unavailable"}
    else:
        state.record(
            "error",
            level="error",
            message="verification probe without payment returned 2xx",
            reservation_id=rid,
        )
        return _escalate(
            deps,
            "unexpected_charge_on_verification",
            f"The no-payment verification probe on {rid} succeeded; check "
            "whether a charge or extension was applied.",
        )

    state.verification = verification
    state.email = email
    state.reservation = reservation
    state.zone = zone

    if verification == "email_unconfirmed":
        # Verify-at-commit fallback: only what quoting needs. Name, vehicle,
        # card and tier stay withheld until the email is confirmed.
        return {
            "outcome": "verified",
            "email_confirmed": False,
            "time_zone": zone_label(zone),
            "current_return_local": format_local(current_return, zone),
            "now_local": format_local(now, zone),
        }

    card = reservation.get("payment", {}).get("card_on_file", {})
    pricing = reservation.get("pricing", {})
    location = reservation["return_location"]
    return {
        "outcome": "verified",
        "email_confirmed": verification == "verified",
        "customer_name": reservation.get("customer_name"),
        "membership_tier": reservation.get("membership_status"),
        "vehicle": reservation.get("vehicle", {}).get("description"),
        "vehicle_type": reservation.get("vehicle", {}).get("type"),
        "return_location": {
            "code": location.get("code"),
            "name": location.get("name"),
            "time_zone": zone_label(zone),
        },
        "current_return_local": format_local(current_return, zone),
        "daily_rate": pricing.get("daily_rate"),
        "currency": pricing.get("currency"),
        "card_type": card.get("type"),
        "card_last_four": card.get("last_four"),
        "now_local": format_local(now, zone),
    }


# -- quote_extension -----------------------------------------------------------


def quote_extension_impl(
    deps: ToolDeps, new_return_local: str, customer_insists: bool = False
) -> dict:
    return _traced(
        deps,
        "quote_extension",
        {"new_return_local": new_return_local, "customer_insists": customer_insists},
        lambda: _quote(deps, new_return_local, customer_insists),
    )


def _alternatives(availability: dict | None) -> dict:
    if not isinstance(availability, dict):
        return {}
    alternatives: dict = {}
    types = (availability.get("availability") or {}).get(
        "alternative_types_at_location"
    )
    if types:
        alternatives["alternative_types_at_location"] = types
    nearby = [
        loc["code"]
        for loc in availability.get("nearby_locations") or []
        if loc.get("code")
    ]
    if nearby:
        alternatives["nearby_locations"] = nearby
    return alternatives


def _quote(deps: ToolDeps, new_return_local: str, customer_insists: bool) -> dict:
    state, settings = deps.state, deps.settings
    blocked = _charge_unknown_block(deps)
    if blocked is not None:
        return blocked
    if not state.is_verified():
        return {"outcome": "verification_required"}

    zone, reservation = state.zone, state.reservation
    try:
        new_return = parse_local(new_return_local, zone)
    except InvalidLocalTime as err:
        return {"outcome": "invalid_datetime", "hint": err.hint}

    current_return = parse_api_datetime(reservation["dates"]["current_return_datetime"])
    current_local = format_local(current_return, zone)
    new_local = format_local(new_return, zone)
    now = deps.now()
    summary = (
        f"Extension of {state.reservation_id} from {current_local} to {new_local} "
        f"({zone_label(zone)})"
    )

    verdict = _guardrail(
        deps,
        "pre_quote",
        pre_quote(current_return, new_return, now, settings, customer_insists),
    )
    if verdict.kind == "invalid":
        return {
            "outcome": "invalid_request",
            "reason": verdict.reason,
            "message": (
                "The new return time must be later than the current return time, "
                f"{current_local}."
            ),
        }
    if verdict.kind == "advise_not_needed":
        return {"outcome": "advise_not_needed", "message": ADVISE_NOT_NEEDED_MESSAGE}
    if verdict.kind == "escalate":
        _void_presented(state)
        return _escalate(deps, verdict.reason, f"{summary}: {verdict.reason}.")

    try:
        availability = deps.client.get_availability(
            reservation["return_location"]["code"],
            reservation["vehicle"]["type"],
            current_return.astimezone(zone).date().isoformat(),
            new_return.date().isoformat(),
        )
    except RoadwayAPIError:
        availability = None
    try:
        verdict = availability_verdict(availability)
    except (KeyError, TypeError):  # malformed body: availability is unknown
        availability, verdict = None, availability_verdict(None)
    _guardrail(deps, "availability", verdict)
    if verdict.kind == "escalate":
        _void_presented(state)
        result = _escalate(
            deps,
            verdict.reason,
            f"{summary}: availability of the vehicle type could not be confirmed.",
        )
        result["alternatives"] = _alternatives(availability)
        return result

    try:
        response = deps.client.quote_extension(
            state.reservation_id, to_api(new_return, zone)
        )
    except RoadwayAPIError as err:
        if _is_client_error(err):
            return {"outcome": "quote_failed", "code": err.code, "message": err.message}
        return {"outcome": "system_unavailable"}
    charges = response["quote"]["charges"]

    verdict = _guardrail(deps, "post_quote", post_quote(charges, settings))
    if verdict.kind == "escalate":
        _void_presented(state)
        return _escalate(
            deps,
            verdict.reason,
            f"{summary}: quoted total {charges.get('total_charged')} "
            f"{charges.get('currency', '')}, {charges.get('extension_days')} days.",
        )

    quote = Quote(
        token=f"q{len(state.quotes) + 1}",
        new_return_utc=new_return.astimezone(timezone.utc),
        new_return_local=new_local,
        charges=charges,
        created_at=now,
    )
    state.add_quote(quote)
    card = _card_if_confirmed(state)
    return {
        "outcome": "quoted",
        "quote_token": quote.token,
        "new_return_local": new_local,
        "time_zone": zone_label(zone),
        "extension_days": charges.get("extension_days"),
        "daily_rate": charges.get("daily_rate"),
        "subtotal": charges.get("subtotal"),
        "late_fee": charges.get("late_fee"),
        "taxes_and_fees": charges.get("taxes_and_fees"),
        "total_charged": charges.get("total_charged"),
        "currency": charges.get(
            "currency", reservation.get("pricing", {}).get("currency")
        ),
        "card_type": card.get("type"),
        "card_last_four": card.get("last_four"),
        "expires_in_minutes": settings.quote_ttl_minutes,
    }


# -- confirm_extension ---------------------------------------------------------


def confirm_extension_impl(deps: ToolDeps, quote_token: str) -> dict:
    # Only the token is traced: CVV and ZIP never leave `_confirm`'s locals.
    return _traced(
        deps,
        "confirm_extension",
        {"quote_token": quote_token},
        lambda: _confirm(deps, quote_token),
    )


def _confirmable(deps: ToolDeps, token: str) -> tuple[Quote | None, dict | None]:
    """The quote to charge, or the refusal to return instead."""
    state = deps.state
    blocked = _charge_unknown_block(deps)
    if blocked is not None:
        return None, blocked
    if not state.is_verified():
        return None, {"outcome": "verification_required"}
    done = next(
        (q for q in state.quotes if q.token == token and q.status == "committed"),
        None,
    )
    if done is not None:
        # Already charged (perhaps in a turn that then failed): report it again,
        # never charge a second time.
        return None, {
            "outcome": "no_valid_quote",
            "reason": "already_committed",
            **(done.committed or {}),
        }
    quote = state.latest_quote()
    if quote is None or quote.token != token:
        known = any(q.token == token for q in state.quotes)
        reason = "superseded" if known else "no_quote"
        return None, {"outcome": "no_valid_quote", "reason": reason}
    if quote.status != "presented":
        return None, {"outcome": "no_valid_quote", "reason": "not_presented"}
    if _expired(deps, quote):
        return None, {"outcome": "no_valid_quote", "reason": "expired"}
    return quote, None


def _expired(deps: ToolDeps, quote: Quote) -> bool:
    ttl = timedelta(minutes=deps.settings.quote_ttl_minutes)
    return not deps.now() - quote.created_at < ttl


def _confirm(deps: ToolDeps, quote_token: str) -> dict:
    state = deps.state
    quote, refusal = _confirmable(deps, quote_token)
    if refusal is not None:
        return refusal

    reservation = state.reservation
    charges = quote.charges
    card = _card_if_confirmed(state)
    currency = charges.get("currency") or reservation.get("pricing", {}).get(
        "currency", "USD"
    )
    quote.attempts += 1
    details = deps.payments.collect(
        charges.get("total_charged"),
        currency,
        card.get("type"),
        card.get("last_four"),
        description=f"Extend {state.reservation_id} to {quote.new_return_local}",
    )
    if details is None:
        quote.status = "declined"
        return {"outcome": "customer_declined"}
    if _expired(deps, quote):  # the customer may have lingered at the prompt
        return {"outcome": "no_valid_quote", "reason": "expired"}

    key = f"ext-{state.session_id}-{quote.token}-{quote.attempts}"
    quote.charge_key = key
    # Until a definite outcome the quote blocks any further quote or charge, so
    # if anything below raises, a retry can never charge again under a new key.
    quote.status = "in_flight"
    try:
        response = deps.client.extend(
            state.reservation_id,
            email=state.email,
            new_return_datetime=to_api(quote.new_return_utc, state.zone),
            payment={
                "use_card_on_file": True,
                "cvv": details.cvv,
                "billing_zip": details.billing_zip,
            },
            idempotency_key=key,
        )
    except RoadwayAPIError as err:
        return _commit_failed(deps, quote, key, err)
    except Exception as exc:  # noqa: BLE001 - the request may have been sent
        quote.status = "unknown"
        return _escalate(
            deps,
            "charge_status_unknown",
            f"{_charge_summary(state, quote)}: the charge outcome is unknown "
            f"({type(exc).__name__}).",
            message=CHARGE_UNKNOWN_MESSAGE,
            idempotency_key=key,
            charge_status="unknown",
        )

    quote.status = "committed"
    charged = response.get("charges") or {}
    extension = response.get("extension_details") or {}
    quote.committed = {
        "confirmation_number": response.get("confirmation_number"),
        "new_return_local": quote.new_return_local,
        "time_zone": zone_label(state.zone),
        "total_charged": charged.get("total_charged"),
    }
    new_return = quote.new_return_utc
    if extension.get("new_return_datetime"):
        new_return = parse_api_datetime(extension["new_return_datetime"])
    new_return_local = format_local(new_return, state.zone)
    quote.committed["new_return_local"] = new_return_local
    # The committed return is the floor for a further extension's guardrails.
    reservation["dates"]["current_return_datetime"] = to_api(new_return, state.zone)
    differs = _amount_differs(charged.get("total_charged"), charges)
    if differs:
        state.record(
            "tool_result",
            level="warning",
            tool="confirm_extension",
            message="charged total differs from the quoted total",
            quoted_total=charges.get("total_charged"),
            charged_total=charged.get("total_charged"),
        )
    return {
        "outcome": "committed",
        "confirmation_number": response.get("confirmation_number"),
        "new_return_local": new_return_local,
        "time_zone": zone_label(state.zone),
        "charges": charged,
        "late_return": extension.get("late_return"),
        "amount_differs_from_quote": differs,
        "card_type": card.get("type"),
        "card_last_four": card.get("last_four"),
    }


def _amount_differs(charged_total, quoted: dict) -> bool:
    quoted_total = quoted.get("total_charged")
    try:
        return abs(float(charged_total) - float(quoted_total)) > 0.01
    except (TypeError, ValueError):  # missing total: cannot confirm it matches
        return True


def _charge_summary(state: ConversationState, quote: Quote) -> str:
    return (
        f"Extension of {state.reservation_id} to {quote.new_return_local} "
        f"(quote {quote.token}, {quote.charges.get('total_charged')} "
        f"{quote.charges.get('currency', '')})"
    )


def _commit_failed(
    deps: ToolDeps, quote: Quote, key: str, err: RoadwayAPIError
) -> dict:
    state, settings = deps.state, deps.settings
    summary = _charge_summary(state, quote)
    if not _is_client_error(err):
        # 5xx, transport failure after retries, or an unreadable 2xx: the charge
        # may have gone through. Never retried, never reported either way.
        quote.status = "unknown"
        return _escalate(
            deps,
            "charge_status_unknown",
            f"{summary}: the charge outcome is unknown ({err.code}).",
            message=CHARGE_UNKNOWN_MESSAGE,
            idempotency_key=key,
            charge_status="unknown",
        )

    if err.code in PAYMENT_ERROR_CODES and err.status in (400, 402):
        state.payment_failures += 1
        limit = settings.max_payment_failures
        if state.payment_failures >= limit:
            quote.status = "voided"
            return _escalate(
                deps,
                "payment_failed",
                f"{summary}: payment failed {state.payment_failures} times "
                f"({err.code}).",
            )
        # The attempt definitively failed: the quote is presented again and the
        # next confirm uses a new attempt number and key.
        quote.status = "presented"
        return {
            "outcome": "payment_failed",
            "code": err.code,
            "attempts_remaining": limit - state.payment_failures,
        }

    if err.status == 403:
        quote.status = "voided"
        state.verification = "unverified"
        state.email_failures += 1
        if state.email_failures >= settings.max_email_failures:
            return _escalate(
                deps,
                "verification_failed",
                f"{summary}: the email did not match at commit; "
                f"{state.email_failures} failed verifications.",
            )
        return {"outcome": "not_verified", "message": NOT_VERIFIED_AT_COMMIT_MESSAGE}

    quote.status = "voided"
    return {"outcome": "rejected", "code": err.code, "message": err.message}


# -- search_knowledge_base -----------------------------------------------------


def search_knowledge_base_impl(deps: ToolDeps, query: str) -> dict:
    deps.state.record("tool_call", tool="search_knowledge_base", args={"query": query})
    result = deps.kb.search(query, k=3)
    # The kb_search event already lists ids and scores; keep bodies out of the log.
    deps.state.record(
        "tool_result",
        tool="search_knowledge_base",
        result={
            "outcome": result["outcome"],
            "fallback": result["fallback"],
            "article_ids": [a["id"] for a in result["articles"]],
        },
    )
    return result


# -- escalate_to_human ---------------------------------------------------------


def escalate_to_human_impl(deps: ToolDeps, reason: str, summary: str) -> dict:
    return _traced(
        deps,
        "escalate_to_human",
        {"reason": reason, "summary": summary},
        lambda: _handoff(deps, reason, summary),
    )


def _handoff(deps: ToolDeps, reason: str, summary: str) -> dict:
    # No verification needed: out-of-scope requests are handed off as they are.
    handoff = deps.handoffs.open(deps.state, reason, summary, now=deps.now())
    return {
        "outcome": "escalated",
        "handoff_reference": handoff["handoff_reference"],
        "already_open": handoff["already_open"],
    }
