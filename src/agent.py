"""The Roadway rental-extension agent: definition, instructions and the CLI chat loop.

Every rule that touches money or identity lives in the tools (`src/tools.py`);
the instructions describe the flow and tone, and the code is the guarantee.

Run it:
    python src/agent.py
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

from agents import Agent, RunContextWrapper, Runner, function_tool
from dotenv import load_dotenv

from config import Settings, load_settings
from handoff import HandoffWriter
from kb import KnowledgeBase, openai_embedder
from payment import CliPaymentCollector, PaymentCollector
from roadway_client import RoadwayClient
from session import CHARGE_PENDING_STATUSES, ConversationState, new_session_id
from telemetry import SessionLogger
from tools import (
    CHARGE_UNKNOWN_MESSAGE,
    ToolDeps,
    confirm_extension_impl,
    escalate_to_human_impl,
    quote_extension_impl,
    search_knowledge_base_impl,
    verify_customer_impl,
)

MAX_TURNS = 20
EXIT_WORDS = {"exit", "quit"}
TURN_ERROR_REPLY = (
    "Sorry, something went wrong on my side. Please say that again, or type "
    "'quit' to leave."
)

INSTRUCTIONS = """\
You are the Roadway Assistant, a customer-support agent for Roadway car rentals.

# Persona
Be concise and warm. Use plain numbers with currency (for example "USD 100.49").
Ask for one thing at a time. Never invent facts: say only what a tool returned or
what a knowledge-base article says.

# Scope
You can fully handle one task: extending an active rental (a later return date or
time, same vehicle, same return location, card on file).

Everything else is out of scope: cancelling, modifying anything other than the
return time, upgrades or a vehicle class change, returning to another location,
paying with another card, complaints, and any request to talk to a person. For
these:
1. If the customer asked a policy question, answer it with search_knowledge_base.
2. Then call escalate_to_human with a short reason (for example "cancellation",
   "upgrade", "complaint", "agent_requested") and a one-line summary.
Do NOT ask for or verify the reservation ID and email first for an out-of-scope
request; hand it off as it is.

Policy questions on their own ("what is the grace period?") need no verification:
answer them from search_knowledge_base.

# Extension flow
1. Collect the reservation ID and the email on file. Say nothing about any booking
   (not even whether it exists or is active) until verify_customer returns
   outcome "verified". Then call verify_customer(reservation_id, email). Each call
   starts a fresh verification, so a new ID or email means a new call.
2. Ask for the new return date and time in the local time of the return location
   (verify_customer returns its time_zone, current_return_local and now_local).
   Resolve relative dates ("tomorrow at 5pm", "Friday noon") from now_local, then
   call quote_extension with new_return_local in the exact format YYYY-MM-DDTHH:MM
   (24-hour clock, local time, no offset), for example 2027-06-17T14:00. If the
   customer's time is ambiguous, ask before quoting.
3. Read the exact quote back: extension_days, subtotal, late_fee (if any),
   taxes_and_fees, total_charged with currency, and the card_type and
   card_last_four it will be charged to. Ask for an explicit yes. Only an explicit
   yes to this quote counts ("yes", "go ahead", "confirm"); anything else is not
   consent. A new date means a new quote and a new yes.
4. On an explicit yes, tell the customer a secure prompt will now ask for the
   card's CVV and billing ZIP, and that they must never type card details in
   chat. Then call confirm_extension(quote_token) with the token from the latest
   quote. Call it once per yes; never call it again for the same yes.
5. On "committed", report the confirmation_number, the new return time
   (new_return_local and time_zone) and the amount charged (charges.total_charged).
   If amount_differs_from_quote is true, say the charged amount differs from the
   quote and give both.

If a customer types a CVV, ZIP or card number in chat, do not repeat it or use
it; remind them never to type card details in chat and that the secure prompt
collects them.

Never promise anything a tool did not return: no extension, availability, price,
waiver, refund or callback time unless a tool result says so.

# Tool outcomes
verify_customer:
- verified: continue the flow. If email_confirmed is false, continue anyway; the
  email is checked again when the extension is committed. In that case the result
  holds only time_zone, current_return_local and now_local: do not describe the
  booking (no name, vehicle, card or membership), and when quoting say
  "the card on file" instead of a card type and last four.
- not_verified: say the details did not match (do not say which), and ask the
  customer to check the reservation ID and email; attempts_remaining tells you how
  many tries are left.
- not_eligible: say this booking cannot be extended here, without describing it,
  and offer a handoff with escalate_to_human.
- system_unavailable: say the system is not responding right now and offer to try
  again shortly or hand off.
- escalated: see "Escalations".
quote_extension:
- quoted: present it as in step 3. It expires after expires_in_minutes.
- verification_required: verify the customer first.
- invalid_datetime: ask for the date and time again, or fix the format yourself
  using the hint (YYYY-MM-DDTHH:MM, local time).
- invalid_request: explain the message (the new return must be after the current
  return) and ask for a later time.
- advise_not_needed: explain that returns within the 30 minutes grace period after
  the scheduled time are not charged a late fee, and that an extension bills a full
  day. Call quote_extension again with customer_insists=true only if the customer
  still insists on extending after hearing this.
- quote_failed: explain the message in plain words and offer a handoff.
- system_unavailable: as above; nothing was charged.
- escalated: see "Escalations". For reason availability_unconfirmed, also mention
  the alternatives returned (other vehicle types or nearby locations), without
  promising them.
confirm_extension:
- committed: report as in step 5.
- no_valid_quote: reason already_committed means this quote was already charged
  and the extension is done: tell the customer so, with the confirmation_number,
  new_return_local and total_charged returned; do not quote or charge it again.
  Any other reason (expired, superseded, no_quote or not_presented): nothing was
  charged; get a fresh quote with quote_extension, read it again and ask for a new
  explicit yes.
- verification_required: verify the customer first.
- customer_declined: nothing was charged; ask whether they want something else.
- payment_failed: the payment did not go through and nothing was charged. Offer
  one more attempt (attempts_remaining); on a new explicit yes call
  confirm_extension again with the same quote_token.
- not_verified: the email did not match at commit and nothing was charged. Ask
  for the reservation ID and email again, call verify_customer, then get a new
  quote and a new yes.
- rejected: nothing was charged; explain the message and offer a handoff.
- escalated: see "Escalations".
- system_unavailable from confirm_extension: do not call confirm_extension again
  and do not say whether a charge happened; say we could not complete the
  confirmation and offer a handoff with escalate_to_human.
search_knowledge_base:
- results: answer from the articles (see "Knowledge base"). If no article answers
  the question, say so and offer a handoff.
escalate_to_human:
- escalated: see "Escalations".

# Escalations
Every outcome "escalated" means a human handoff record exists. Always read the
handoff_reference to the customer and say a representative will follow up. If
already_open is true, say the request is already with a representative under that
same reference; do not open another handoff for the same problem. Use the
message field when there is one. By reason:
- charge_status_unknown: say we could not confirm whether the extension went
  through, it will not be retried, and a representative will check. Never say it
  succeeded or failed. After this, quote_extension and confirm_extension return
  this same handoff (already_open true): do not try to quote or charge again in
  this conversation.
- verification_failed: say we could not verify the details and a representative
  will help.
- payment_failed: say the payment did not go through and a representative will
  help complete it.
- overdue_rental, extension_too_long, high_value, availability_unconfirmed,
  unexpected_charge_on_verification: say this extension needs a representative
  and give the reference; do not give a reason the result does not state.

# Knowledge base
Use search_knowledge_base for every policy question. Articles have an authority:
official-policy outranks help-center, which outranks legacy. When articles
disagree, follow the higher authority and never quote a legacy figure that an
official-policy article contradicts. For example, the grace period for late
returns is 30 minutes per kb_ext_01, not 2 hours. Cite the article IDs you used
in brackets, for example [kb_ext_01].

Answer every policy question in these steps, all within the same turn:
1. Call search_knowledge_base with the customer's question.
2. Read the bodies. Does an official-policy or help-center article state the
   exact figure asked for (a duration, amount or limit)? Phrases like "the
   applicable grace period", "a short grace period" or "see the extension
   policy, which is authoritative" do NOT state a figure.
3. If not, you MUST call search_knowledge_base again before replying, with a
   query naming the policy the articles point to and the fact, for example
   "extension policy grace period".
4. Only then reply. Never tell the customer a figure is not stated, or that you
   would need to check another policy, unless that second search also came back
   without it.
Cite only articles whose text states the fact you quote; never cite an article
for a figure it does not contain. Prefer official-policy articles, and
never cite a legacy article over an official-policy one.
"""


# -- tools -----------------------------------------------------------------------


def guarded(deps: ToolDeps, tool: str, call: Callable[[], dict]) -> dict:
    """Run a tool body; an unexpected exception is logged and reported to the
    model as `system_unavailable` instead of crashing the turn."""
    try:
        return call()
    except Exception as exc:  # noqa: BLE001 - any bug must not reach the model raw
        deps.state.record(
            "error",
            level="error",
            tool=tool,
            message=f"{type(exc).__name__}: {exc}",
        )
        return {"outcome": "system_unavailable"}


@function_tool
def verify_customer(
    ctx: RunContextWrapper[ToolDeps], reservation_id: str, email: str
) -> dict:
    """Verify the customer owns the reservation. Call before saying anything about
    a booking.

    Args:
        reservation_id: The reservation ID, for example RWR-29471835.
        email: The email address on file for the reservation.

    Returns a dict whose `outcome` is one of: verified (booking summary,
    email_confirmed, now_local), not_verified (attempts_remaining), not_eligible,
    system_unavailable, escalated (reason, handoff_reference, already_open).
    """
    deps = ctx.context
    return guarded(
        deps,
        "verify_customer",
        lambda: verify_customer_impl(deps, reservation_id, email),
    )


@function_tool
def quote_extension(
    ctx: RunContextWrapper[ToolDeps],
    new_return_local: str,
    customer_insists: bool = False,
) -> dict:
    """Quote extending the verified reservation to a new return time.

    Args:
        new_return_local: The new return as local time at the return location,
            exactly YYYY-MM-DDTHH:MM (24-hour, no offset), e.g. 2027-06-17T14:00.
        customer_insists: True only when the customer still wants a short
            extension after hearing the advise_not_needed explanation.

    Returns a dict whose `outcome` is one of: quoted (quote_token, extension_days,
    subtotal, late_fee, taxes_and_fees, total_charged, currency, card_type,
    card_last_four), verification_required, invalid_datetime (hint),
    invalid_request (reason, message), advise_not_needed (message), quote_failed
    (code, message), system_unavailable, escalated (reason, handoff_reference,
    already_open, alternatives for availability_unconfirmed).
    """
    deps = ctx.context
    return guarded(
        deps,
        "quote_extension",
        lambda: quote_extension_impl(deps, new_return_local, customer_insists),
    )


@function_tool
def confirm_extension(ctx: RunContextWrapper[ToolDeps], quote_token: str) -> dict:
    """Commit the latest quote after the customer's explicit yes. A secure prompt
    outside the chat collects the CVV and billing ZIP and charges the card on file.

    Args:
        quote_token: The quote_token of the latest quoted result.

    Returns a dict whose `outcome` is one of: committed (confirmation_number,
    new_return_local, charges, amount_differs_from_quote), no_valid_quote
    (reason: no_quote, superseded, not_presented, expired, or already_committed
    with confirmation_number, new_return_local, total_charged),
    verification_required,
    customer_declined, payment_failed (attempts_remaining), not_verified, rejected
    (code, message), escalated (reason, handoff_reference, already_open, message).
    """
    deps = ctx.context
    return guarded(
        deps,
        "confirm_extension",
        lambda: confirm_extension_impl(deps, quote_token),
    )


@function_tool
def search_knowledge_base(ctx: RunContextWrapper[ToolDeps], query: str) -> dict:
    """Search the Roadway help-center knowledge base for policy answers.

    Args:
        query: The customer's question in a few words.

    Returns {"outcome": "results", "fallback": bool, "articles": [...]}; each
    article has id, title, authority (official-policy, help-center or legacy),
    last_updated and body.
    """
    deps = ctx.context
    return guarded(
        deps,
        "search_knowledge_base",
        lambda: search_knowledge_base_impl(deps, query),
    )


@function_tool
def escalate_to_human(
    ctx: RunContextWrapper[ToolDeps], reason: str, summary: str
) -> dict:
    """Hand the conversation to a human representative. Needs no verification.

    Args:
        reason: A short snake_case reason, e.g. cancellation, upgrade, complaint,
            agent_requested.
        summary: One line describing what the customer needs.

    Returns {"outcome": "escalated", "handoff_reference": str,
    "already_open": bool}. Read the handoff_reference to the customer.
    """
    deps = ctx.context
    return guarded(
        deps,
        "escalate_to_human",
        lambda: escalate_to_human_impl(deps, reason, summary),
    )


TOOLS = [
    verify_customer,
    quote_extension,
    confirm_extension,
    search_knowledge_base,
    escalate_to_human,
]


# -- wiring ----------------------------------------------------------------------


def build_agent(settings: Settings) -> Agent[ToolDeps]:
    return Agent[ToolDeps](
        name="Roadway Assistant",
        instructions=INSTRUCTIONS,
        model=settings.openai_model,
        tools=list(TOOLS),
    )


def build_deps(
    settings: Settings, payments: PaymentCollector, session_id: str | None = None
) -> ToolDeps:
    session_id = session_id or new_session_id()
    log_dir = Path(settings.log_dir)
    state = ConversationState(session_id, logger=SessionLogger(session_id, log_dir))
    client = RoadwayClient.from_settings(
        settings, on_attempt=lambda a: state.record("api_attempt", **a)
    )
    kb = KnowledgeBase.load(
        Path(settings.kb_path),
        embed=openai_embedder(settings.embedding_model),
        cache_path=Path(settings.cache_dir) / "kb_embeddings.json",
        on_event=state.record,
    )
    return ToolDeps(
        state=state,
        client=client,
        payments=payments,
        kb=kb,
        handoffs=HandoffWriter(log_dir),
        settings=settings,
    )


def run_turn(
    agent: Agent[ToolDeps],
    deps: ToolDeps,
    history: list,
    user_text: str,
    runner=Runner,
) -> tuple[str, list]:
    deps.state.record("turn_user", text=user_text)
    result = runner.run_sync(
        agent,
        history + [{"role": "user", "content": user_text}],
        context=deps,
        max_turns=MAX_TURNS,
    )
    reply = str(result.final_output)
    deps.state.record("turn_agent", text=reply)
    return reply, result.to_input_list()


def respond(
    agent: Agent[ToolDeps],
    deps: ToolDeps,
    history: list,
    text: str,
    runner=Runner,
) -> tuple[str, list]:
    """One customer message: the reply and the new history. Never raises."""
    before = {q.token: q.status for q in deps.state.quotes}
    try:
        return run_turn(agent, deps, history, text, runner=runner)
    except Exception as exc:  # noqa: BLE001 - keep the session alive
        deps.state.record(
            "error", level="error", message=f"{type(exc).__name__}: {exc}"
        )
        reply = charge_outcome_reply(deps, before)
        if reply is None:
            return TURN_ERROR_REPLY, history
        # keep the outcome in the model's history for later turns
        return reply, history + [
            {"role": "user", "content": text},
            {"role": "assistant", "content": reply},
        ]


def chat_loop(
    agent: Agent[ToolDeps],
    deps: ToolDeps,
    input_fn: Callable[[str], str] = input,
    print_fn: Callable[[str], None] = print,
    runner=Runner,
) -> None:
    logger = deps.state.logger
    log_path = logger.path if logger is not None else "(not written)"
    print_fn(f"Session {deps.state.session_id}; log: {log_path}")
    print_fn("Roadway Assistant: Hi! How can I help with your rental today?")
    history: list = []
    while True:
        try:
            text = input_fn("You: ").strip()
        except EOFError:
            return
        if not text:
            continue
        if text.lower() in EXIT_WORDS:
            return
        reply, history = respond(agent, deps, history, text, runner=runner)
        print_fn(f"Roadway Assistant: {reply}")


def charge_outcome_reply(deps: ToolDeps, before: dict[str, str]) -> str | None:
    """After a failed turn: the charge outcome reached during it, if any, so the
    customer is never left unaware of a charge (or told to just try again)."""
    state = deps.state
    changed = [q for q in state.quotes if before.get(q.token) != q.status]
    for quote in reversed(changed):
        if quote.status == "committed" and quote.committed:
            c = quote.committed
            currency = quote.charges.get("currency", "USD")
            return (
                "Your extension is confirmed. Confirmation number: "
                f"{c.get('confirmation_number')}. New return: "
                f"{c.get('new_return_local')} ({c.get('time_zone')}). "
                f"Amount charged: {currency} {c.get('total_charged')}."
            )
    for quote in reversed(changed):
        if quote.status in CHARGE_PENDING_STATUSES:
            quote.status = "unknown"
            reference = state.handoffs.get("charge_status_unknown")
            if reference is None:
                try:
                    reference = deps.handoffs.open(
                        state,
                        "charge_status_unknown",
                        f"Charge for quote {quote.token} interrupted by an error.",
                        idempotency_key=quote.charge_key,
                        charge_status="unknown",
                        now=deps.now(),
                    )["handoff_reference"]
                except Exception:  # noqa: BLE001 - still tell the customer
                    reference = None
            tail = f" The reference is {reference}." if reference else ""
            return CHARGE_UNKNOWN_MESSAGE + tail
    return None


def main() -> None:
    load_dotenv()
    missing = [
        k for k in ("OPENAI_API_KEY", "ROADWAY_API_KEY") if not os.environ.get(k)
    ]
    if missing:
        print(f"Set {' and '.join(missing)} in your .env first (see env.example).")
        return
    settings = load_settings()
    deps = build_deps(settings, CliPaymentCollector())
    chat_loop(build_agent(settings), deps)


if __name__ == "__main__":
    main()
