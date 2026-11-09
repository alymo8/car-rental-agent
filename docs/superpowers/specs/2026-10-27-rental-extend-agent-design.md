# Roadway rental-extension agent — design

- **Status:** approved
- **Date:** 2026-10-27

## Purpose

Roadway wants to know whether an AI agent can take a meaningful share of rental-servicing
volume off human agents "without putting revenue or customer trust at risk", starting with
a narrow pilot that does a few things well (`PROBLEM.md`). This spec defines a working
prototype that fully handles **one** workflow, **extending an active rental**, against the
mock Roadway API (`docs/api-reference.md`), answers servicing policy questions from the
knowledge base (`data/knowledge-base/articles.json`), and hands every other request to a
human with a structured summary.

Why Extend: it is the highest-volume servicing request, it is revenue-positive for Roadway,
it saves the customer money versus returning late (KB `kb_ext_05`, `kb_fee_02`), and it has
a clean, auditable shape (lookup → verify → quote → confirm → charge). Cancel destroys
revenue and is irreversible; Modify largely overlaps Extend apart from one-way fees and
location availability; Upgrade is eligibility-gated by rules the API owns. Those three are
deliberately left to humans in the pilot.

## Scope

**In:**

- `src/roadway_client.py` — rewritten as a resilient API client (timeouts, retries,
  idempotency, typed errors, per-attempt logging).
- `src/config.py` (new) — thresholds and settings, overridable by environment variables.
- `src/timeutil.py` (new) — location time zones and datetime normalisation.
- `src/guardrails.py` (new) — pure functions for the extension policy checks.
- `src/session.py` (new) — per-conversation state: verification, quotes, counters, trace.
- `src/kb.py` (new) — authority-aware retrieval over the knowledge base.
- `src/payment.py` (new) — card-detail collection outside the model.
- `src/handoff.py` (new) — structured handoff records.
- `src/telemetry.py` (new) — JSON-lines logging with redaction.
- `src/tools.py` (new) — the agent's function tools.
- `src/agent.py` — the agent definition, instructions and the CLI chat loop.
- `tests/` (new) — unit and fault-injection tests (pytest, no network).
- `evals/` (new) — scripted end-to-end conversations against the live API and model.
- `README.md` — rewritten as the project overview.
- `requirements.txt`, `.gitignore`, `env.example` — dependencies, ignored log and cache
  folders (`logs/` (new), `.cache/` (new)), new optional settings.

**Out:**

- Executing Modify, Cancel or Upgrade (the agent explains policy from the KB, then hands off).
- Changing vehicle class, extending into a different return location, or paying with a
  card other than the card on file.
- A web or voice UI, multiple languages, authentication beyond the email on file.
- Real routing to a human queue (handoffs are written to disk and announced to the customer).
- Persisting state across conversations or processes.

## Design

### Shape

One OpenAI Agents SDK agent owns the conversation. **Every rule that touches money or
identity is enforced in Python inside the tools, not in the prompt**: the model can ask for
anything, and the tools refuse what policy does not allow. The prompt describes the flow
and tone; the code is the guarantee.

Conversation flow:

1. Customer states intent. Out-of-scope intents (cancel, modify, upgrade, class change,
   complaints, "talk to a person") → answer any policy question from the KB, then
   `escalate_to_human`.
2. For an extension, the agent collects the **reservation ID and the email on file**
   before disclosing anything about the booking, and calls `verify_customer`.
3. Customer gives the new return date/time (in the return location's local time). The
   agent calls `quote_extension`.
4. The agent presents the exact quote (days, subtotal, late fee if any, taxes, total, card
   type and last four) and asks for an explicit yes.
5. On yes, the agent calls `confirm_extension`. The code opens a **masked payment prompt
   outside the model** that restates the amount and asks for CVV and billing ZIP; entering
   them is the customer's authorization. The code then commits the extension.
6. The agent reports the confirmation number, new return time and amount charged.

### Tools (`src/tools.py`)

All tools receive the session state through the SDK run context and append a trace event.
Tool results are short JSON-able dicts with an `outcome` field the prompt keys off.

- **`verify_customer(reservation_id, email)`**
  - Normalises the ID (upper-case, trimmed) and email (trimmed, lower-case).
  - **Nothing about the booking, including whether it exists or is active, is disclosed
    before the email is confirmed.** `GET /reservations/{id}` runs first (internally,
    to compute the probe date); a 404 returns `outcome: not_verified` (the same result
    as a wrong email, and it counts toward `email_failures`).
  - **Email check without a charge:** `POST /reservations/{id}/extend` with the email, a
    `new_return_datetime` of (later of current return and now) + 1 day, **no `payment`
    block**, and an `Idempotency-Key` of `verify-<session_id>-<attempt>`. Verified on probe
    (2026-10-27): the API checks the email before payment, so `403 VERIFICATION_FAILED` =
    wrong email and `400 PAYMENT_VALIDATION_ERROR` = email correct.
    - `403` → `outcome: not_verified`.
    - `409 RESERVATION_NOT_ACTIVE` (or the GET showed a status other than `active` and
      the probe did not return 403) → `outcome: not_eligible`; the agent says the
      booking cannot be changed here and offers a handoff, without describing it.
    - Any other 4xx on an `active` booking → fall back to **verify-at-commit**: mark the
      session `email_unconfirmed`, continue, and let `confirm_extension` surface a 403.
    - 5xx after retries → `outcome: system_unavailable`.
    - A 2xx (should be impossible without payment) → log at `error` level, open a handoff
      with reason `unexpected_charge_on_verification`, and return `outcome: escalated`.
  - Wrong email increments `email_failures`; at 2 → automatic handoff
    (`verification_failed`).
  - Verified → `outcome: verified`, with the reservation summary: name, membership tier, vehicle, return
    location (code, name, local time zone), current return time in local time, daily rate,
    card type and last four, and the **current local time at the return location** (so the
    model can resolve "tomorrow at 5").
- **`quote_extension(new_return_local)`**
  - Requires a verified session (or `email_unconfirmed`).
  - Parses `YYYY-MM-DDTHH:MM` as local time at the return location (`src/timeutil.py`);
    rejects anything else with `outcome: invalid_datetime` and a hint.
  - Runs the **pre-quote guardrails** (below). Then checks availability with
    `GET /availability` for the vehicle type at the return location from the current return
    date to the new return date.
  - `POST /reservations/{id}/quote` with `change_type: extend` and the new return time
    **always sent with an explicit UTC offset** (probe: the API accepts and silently
    mis-handles offset-less times).
  - Runs the **post-quote guardrails** on the returned charges.
  - Stores a `Quote` (token, new return UTC and local, charges, created time, status
    `presented`) on the session and returns it. Only the latest quote is confirmable; a new
    quote supersedes earlier ones.
- **`confirm_extension(quote_token)`**
  - Refuses (`outcome: no_valid_quote`) unless the token is the session's latest quote,
    status `presented`, and younger than `QUOTE_TTL_MINUTES` (15).
  - Calls the `PaymentCollector` (see below) with the amount, card type and last four.
    The customer can decline (empty input) → quote status `declined`,
    `outcome: customer_declined`, nothing charged.
  - `POST /reservations/{id}/extend` with email, new return time, and
    `payment: {use_card_on_file: true, cvv, billing_zip}`. One `Idempotency-Key` per
    payment attempt: `ext-<session_id>-<quote_token>-<attempt>`, reused across transport
    retries of that attempt.
  - Outcomes:
    - 2xx → quote status `committed`; returns confirmation number, new return (local),
      charges, `late_return`. If the charged total differs from the quote by more than
      $0.01, the result says so and the event is logged at `warning`.
    - `402 PAYMENT_DECLINED` / `400 PAYMENT_VALIDATION_ERROR` → increment
      `payment_failures`; at 2 → automatic handoff (`payment_failed`); otherwise
      `outcome: payment_failed` and the agent may offer one more attempt (new attempt
      number, new key: the earlier attempt definitively failed).
    - `403 VERIFICATION_FAILED` (verify-at-commit path) → same handling as a wrong email.
    - `409 RESERVATION_NOT_ACTIVE`, `400 INVALID_EXTENSION`, `400 INVALID_DATE` →
      `outcome: rejected` with the API message; agent explains and offers a handoff.
    - Still 5xx or timeout after retries → **outcome unknown**: quote status
      `unknown`, automatic handoff (`charge_status_unknown`) carrying the idempotency key;
      the agent tells the customer it cannot confirm whether the change went through, will
      not retry, and a representative will check. **Never** reported as success or failure.
  - CVV and ZIP live only in local variables of this function; they are never returned,
    logged or put in the model context.
- **`search_knowledge_base(query)`** → top 3 articles (id, title, authority, last_updated,
  body), see Retrieval.
- **`escalate_to_human(reason, summary)`** → writes a handoff record and returns the
  reference the agent reads to the customer.

### Guardrails (`src/guardrails.py`)

Pure functions over (reservation, requested new return, now, quote) returning
`allow`, `invalid(<reason>)`, `advise_not_needed`, or `escalate(<reason>)`. Thresholds in
`src/config.py`. `invalid` maps to a `quote_extension` result of
`outcome: invalid_request` with no handoff and no quote call.

| Check | When | Result | Source |
|---|---|---|---|
| Already overdue: now > current return + 30 min grace | pre-quote | `escalate(overdue_rental)` | `kb_ext_01` (30-min grace), `kb_elig_01` ("overdue beyond policy") |
| New return not later than current return | pre-quote | `invalid` (agent explains) | API `INVALID_CHANGE` (observed on probe; undocumented) |
| Extension ≤ 30 min | pre-quote | `advise_not_needed` unless the call passes `customer_insists=true` | API bills a full day (probe: +20 min → $50.24); `kb_ext_03` |
| Extension > `MAX_EXTENSION_DAYS` (14) | pre-quote (by date) and post-quote (by `extension_days`) | `escalate(extension_too_long)` | assumption: approval threshold |
| Quote `total_charged` > `MAX_AUTO_CHARGE_USD` (500) | post-quote | `escalate(high_value)` | `kb_sup_01` ("unusually high-value"); probe: an overdue rental quoted $5,356.25 |
| Requested vehicle type not available, or availability unknown after retries | pre-quote | `escalate(availability_unconfirmed)` with any alternatives returned | `kb_ext_04`, `kb_mod_04` |

`quote_extension` takes an optional `customer_insists: bool` used only for the ≤30-min
rule. Escalations from guardrails create the handoff automatically; the tool result tells
the agent what to say.

### Time zones (`src/timeutil.py`)

- A location-code → IANA zone map for known codes (LAX, SFO, OAK, SNA, SAN, SEA, PDX, LAS,
  PHX, DEN, DFW, IAH, ORD, ATL, MIA, MCO, JFK, LGA, EWR, BOS, IAD, DCA, PHL, …).
- Unknown code → fall back to the UTC offset of the reservation's `pickup_datetime` (probe:
  `current_return_datetime` can arrive in UTC, e.g. RWR-77001020, so it is not trusted for
  the zone).
- `zoneinfo` with the `tzdata` package (required on Windows).

### Retrieval (`src/kb.py`)

- 30 articles; embeddings with `text-embedding-3-small`, computed once and cached in
  `.cache/kb_embeddings.json` keyed by a hash of each article's title and body.
- Score = cosine similarity + authority bonus (`official-policy` +0.05, `help-center` 0,
  `legacy` −0.05). Each result carries its `authority` and `last_updated`.
- If the embeddings call fails, fall back to keyword-overlap scoring with the same
  authority bonus, and log the fallback. In the fallback, a `legacy` article is dropped
  from the results whenever an `official-policy` article in the same `category` also
  matches the query (word overlap alone cannot be trusted to rank the official article
  first).
- With embeddings, a `legacy` article is dropped from the ranked results whenever an
  `official-policy` article in the same `category` ranks above it (every article has a
  score, so "also matches" means "ranks above"); the next result takes its place. A
  legacy article is shown only when it outranks every official article in its category,
  or its category has none.
- The prompt instructs: official policy overrides help-center and legacy; cite article IDs;
  never quote a legacy figure when an official article states it (known conflict: grace
  period, `kb_fee_01` legacy "2 hours" vs `kb_ext_01` official "30 minutes").

### API client (`src/roadway_client.py`)

- One `requests.Session`, injectable for tests. `X-API-Key` on every request.
- Timeouts: connect 3 s; read 10 s (12 s for `/availability`).
- Retries: up to 3 attempts on 5xx, timeouts and connection errors, exponential backoff
  with jitter (0.5 s, 1 s, cap 4 s). **No retry on 4xx.** Writes are retried only when an
  `Idempotency-Key` is set, and every retry reuses it.
- Errors raise `RoadwayAPIError(status, code, message, details, retryable)`; transport
  failures after retries raise it with `status=None`, `code="TRANSPORT_ERROR"`.
- Every attempt is logged: method, path, status, error code, latency, attempt number,
  idempotency key; never the request body's payment or email.

### Payment collection (`src/payment.py`)

`PaymentCollector` protocol with `collect(amount, currency, card_type, last_four,
description) -> PaymentDetails | None`. The CLI implementation uses `getpass` for CVV and
billing ZIP after printing the exact charge; empty input = decline. Basic format check
(CVV 3–4 digits, ZIP 5 digits) with one re-prompt. Evals and tests use a scripted
collector. In production this would be a secure payment link or IVR capture.

### Handoffs (`src/handoff.py`)

`logs/handoffs/<session_id>-<n>.json` with: reference (`HO-<session>-<n>`), timestamp,
reason code, free-text summary, reservation ID, verified flag, masked email, latest quote
(if any), idempotency key and charge status (if any), and the last 10 trace events. The
customer is told a representative will follow up and given the reference.

### Logging (`src/telemetry.py`)

- `logs/sessions/<session_id>.jsonl`: one JSON object per event (`ts`, `session_id`,
  `event`, `level`, fields). Events: `turn_user`, `turn_agent`, `tool_call`,
  `tool_result`, `api_attempt`, `guardrail`, `kb_search`, `handoff`, `error`.
- Redaction applied to every event: emails masked (`d***@example.com`); any key named
  `cvv`, `billing_zip` or `payment` dropped.
- The CLI prints the session ID and log path at start.

### Agent and CLI (`src/agent.py`)

- Model from `OPENAI_MODEL`, default `gpt-5.4-mini`.
- Instructions cover: persona and tone (concise, warm, plain numbers); scope; the
  extension flow above; never reveal booking details before `verify_customer` succeeds;
  always read the exact quote and get an explicit yes; never promise outcomes the tools
  did not return; KB precedence and citation; how to handle each `outcome`.
- `python src/agent.py` starts a chat loop: prints a session ID, reads lines until
  `exit`/`quit`/EOF, keeps conversation history across turns, logs each turn.

### Evals (`evals/`)

`python -m evals.run_evals [--scenario NAME] [--runs N]` runs scripted conversations: each
scenario is a fixed list of customer messages plus a scripted payment collector, driven
through the real agent, model and live API. Each scenario asserts on the session's
**trace** (tools called, outcomes, number of committed extensions, handoff reason), not on
wording, except the KB scenario which checks the answer text matches `30[- ]minute` and
does not contain "2 hours". Prints a pass/fail table and writes it to `logs/evals/<timestamp>.json`.

## Decisions

1. **Extend only, done deeply** (user decision). Rejected: breadth across all four
   workflows at lower depth.
2. **Guardrails in code, not in the prompt.** Rejected: prompt-only rules (one hallucinated
   tool call could charge a customer).
3. **Escalation thresholds: total > $500 or > 14 days; overdue past the 30-minute grace
   always escalates** (user decision). These are assumptions, not Roadway policy, and are
   configurable. Rejected: $1,000/30 days; overdue check only.
4. **Card details collected by a masked prompt outside the model** (user decision); the
   masked prompt restating the amount is also the deterministic authorization step.
   Rejected: card details typed in chat with log masking.
5. **Early email verification via a no-payment extend call** (user decision), with
   verify-at-commit as the fallback. Rejected: verify only at commit time.
6. **Availability unknown or unavailable → no charge, hand off** (user decision). Rejected:
   rely on the extend call (probes showed it does not check availability).
7. **One idempotency key per payment attempt, reused across retries;** an unresolved 5xx on
   a write is reported as "unknown", never retried with a new key. Probe: two extend calls
   without a key produced two confirmation numbers.
8. **Extensions of 30 minutes or less are advised against** (the grace period covers
   them), but allowed if the customer insists.
9. **Embeddings + authority bonus with a keyword fallback.** Rejected: putting the whole
   KB in the prompt (works at 30 articles but does not scale and loses provenance);
   a vector DB (unneeded at this size). On both paths a legacy article is dropped when a
   same-category official-policy article ranks above it (embeddings) or also matches
   (keyword fallback). Rejected: relying on the −0.05 bonus alone, which live runs on
   2026-10-27 showed still lets the legacy `kb_fee_01` ("2 hours") reach the top 3.
10. **Model `gpt-5.4-mini`** (my call): low cost and latency, strong tool calling;
    configurable.
11. **Scripted customers in evals, assertions on the tool trace.** Rejected: an LLM-
    simulated customer (less repeatable) and wording checks (brittle).

### Assumptions

- The mock's behaviour observed on 2026-10-27 holds: email checked before payment;
  extend does not check availability; quotes accept offset-less datetimes.
- "Now" is the real clock; RWR-77001020's return time is generated relative to now.
- The 30-minute grace period (`kb_ext_01`, official, 2026-04-10) is current; the 2-hour
  figure (`kb_fee_01`) is legacy.
- The email on file is sufficient identity for the pilot.

## Success criteria

1. **Unit tests pass**: `pytest` exits 0 and covers, at minimum:
   - client: retries on 5xx/timeout with backoff; no retry on 4xx; write retries only with
     an idempotency key and reuse the same key; `RoadwayAPIError` fields.
   - guardrails: each row of the guardrail table, including the boundaries (exactly 30
     min, exactly 14 days, exactly $500).
   - `confirm_extension`: refuses without a token, with a stale or superseded token, after
     decline; unknown outcome produces a handoff and no second key.
   - timeutil: known code, unknown code fallback (a fixed offset; DST-correctness applies
     to mapped codes only), DST-correct offsets for mapped codes, UTC input.
   - kb (offline, stubbed embeddings): at equal similarity the authority bonus orders
     official-policy > help-center > legacy; with embeddings unavailable, the keyword
     fallback returns results for "grace period for late returns", does not include
     `kb_fee_01` (legacy, category `fees`) because `kb_fee_02` (official-policy, `fees`)
     also matches, and includes `kb_ext_01` among the results; with stubbed embeddings,
     a legacy article ranked below a same-category official article is dropped and the
     next result fills its slot, while a legacy article that outranks it, or has no
     official article in its category, is kept.
     The live ranking with real embeddings is checked by the `grace_question` scenario.
   - telemetry: emails masked; `cvv`, `billing_zip`, `payment` never written.
2. **Fault injection passes**: a pytest test runs 200 simulated `confirm_extension` calls
   against a fake transport failing 30% of attempts (5xx or timeout). Bar: zero quotes with
   more than one successful charge, zero keys changed within an attempt, and every call
   ends in `committed`, a handled failure, or an `unknown` handoff.
3. **Scenario evals pass**: one invocation of `python -m evals.run_evals --runs 2`
   against the live API and model in which every scenario passes (or is
   `skipped (precondition)`) in both runs:

   | Scenario | Reservation | Expected trace |
   |---|---|---|
   | `preferred_happy` | RWR-29471835 +2 days | verified; quote total $100.49, late fee 0; 1 commit |
   | `standard_happy` | RWR-66002030 +2 days | verified; 1 commit |
   | `due_soon` | RWR-77001020 +2 days | verified; 1 commit |
   | `overdue` | RWR-48372915 | handoff `overdue_rental`; 0 commits |
   | `availability_timeout` | RWR-99004050 +2 days | handoff `availability_unconfirmed`; 0 commits |
   | `declines_quote` | RWR-50000001 +2 days, customer says no | quote presented; 0 commits |
   | `wrong_email_twice` | RWR-66002030, two wrong emails | handoff `verification_failed`; 0 commits |
   | `grace_question` | none | KB searched; the top result of the first `kb_search` is an official-policy article, ranked above `kb_fee_01` (if present); answer matches `30[- ]minute` and not `2 hours`; 0 commits |
   | `cancel_request` | RWR-29471835 | KB searched; `escalate_to_human` called; 0 commits |
   | `tiny_extension` | RWR-66002030 +20 min | `advise_not_needed`; 0 commits |

   `grace_question` checks that official policy outranks the legacy article, not which
   official article ranks first. Live runs on 2026-10-27 ranked `kb_fee_02` or `kb_ext_05`
   first, with `kb_ext_01` outside the top 3. Those two articles refer to "the applicable
   grace period" without its length, which only `kb_ext_01` (official) states correctly
   (legacy `kb_fee_01` gives the outdated 2 hours); the answer's
   30 minutes then comes from the agent's instructions, which name it.

   The `availability_timeout` scenario depends on the mock's OAK/SUV availability timing
   out (seen twice on 2026-10-27). If it answers instead, the scenario is reported as
   `skipped (precondition)` and the unit test of that guardrail stands in for it.
4. **Manual check**: run `python src/agent.py` through three conversations (happy path,
   overdue, policy question); confirm the session log and handoff files exist under
   `logs/`, contain no CVV/ZIP and mask emails; paste the transcripts into the README.
5. **README** covers design decisions, chosen workflow and why, what was cut, how to run
   (setup, chat, tests, evals), and where logs are — checked by reading it against this list.
