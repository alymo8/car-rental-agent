# Roadway rental-extension agent

A CLI agent (OpenAI Agents SDK) that fully handles one rental-servicing workflow,
**extending an active rental**, against the mock Roadway API. It answers servicing policy
questions from the knowledge base and hands every other request to a human with a
structured handoff record. The task is in [`PROBLEM.md`](PROBLEM.md); the full design is in
[`docs/superpowers/specs/2026-10-27-rental-extend-agent-design.md`](docs/superpowers/specs/2026-10-27-rental-extend-agent-design.md).

## What's in the repo

- `src/`: the agent, its tools and guardrails, and the local web demo console.
- `tests/`: 181 network-free unit and fault-injection tests.
- `evals/`: 10 scripted live conversations, scored on the tool trace.
- `docs/superpowers/`: the design specs, implementation plans and review-gate records
  written before the code (built with Claude Code). The two specs hold the full reasoning
  behind this README.
- `data/knowledge-base/`: the help-center articles the agent retrieves from.

## Overview

The model owns the conversation, but **every rule that touches money or identity is
enforced in Python inside the tools, not in the prompt**. The model can ask for anything;
the tools refuse what policy does not allow.

Flow: lookup, verify, quote, confirm, charge.

1. The customer states intent. Out-of-scope requests get a KB answer and a handoff.
2. The agent collects the reservation ID and the email on file and calls `verify_customer`
   before saying anything about the booking.
3. The customer gives the new return time (in the return location's local time); the agent
   calls `quote_extension` and reads out the exact quote.
4. On an explicit yes, `confirm_extension` opens a masked payment prompt **outside the
   model** that restates the amount and asks for CVV and billing ZIP. Entering them is the
   customer's authorization; the code then commits the extension.
5. The agent reports the confirmation number, new return time and amount charged.

Code map (`src/`): `roadway_client.py` (resilient HTTP client), `config.py`, `timeutil.py`,
`guardrails.py`, `session.py`, `kb.py`, `payment.py`, `handoff.py`, `telemetry.py`,
`tools.py`, `agent.py` (agent definition and chat loop). `tests/` is unit and
fault-injection tests (no network); `evals/` is live scripted conversations.

## Chosen workflow and why: Extend

Roadway wants to move volume off human agents "without putting revenue or customer trust at
risk". Extend is the best first workflow for that:

- It is the highest-volume servicing request.
- It is revenue-positive for Roadway and saves the customer money versus returning late
  (KB `kb_ext_05`, `kb_fee_02`).
- It has a clean, auditable shape (lookup, verify, quote, confirm, charge).

The alternatives are left to humans in the pilot. Cancel destroys revenue and cannot be
undone. Modify largely overlaps Extend apart from one-way fees and location availability.
Upgrade is gated by eligibility rules the API owns.

## What was cut

- Executing Modify, Cancel or Upgrade (the agent explains policy from the KB, then hands off).
- Changing vehicle class, extending into a different return location, or paying with a
  card other than the card on file.
- A voice UI, multiple languages, authentication beyond the email on file (a local web
  demo console exists; see How to run).
- Real routing to a human queue (handoffs are written to disk and announced to the customer).
- Persisting state across conversations or processes.

## Design decisions

1. **Extend only, done deeply.** Rejected: breadth across all four workflows at lower depth.
2. **Guardrails in code, not in the prompt.** Rejected: prompt-only rules (one hallucinated
   tool call could charge a customer).
3. **Escalation thresholds: total over $500 or over 14 days; overdue past the 30-minute
   grace always escalates.** These are assumptions, not Roadway policy, and are configurable.
4. **Card details collected by a masked prompt outside the model.** The prompt restating
   the amount is also the deterministic authorization step. Rejected: card details typed in
   chat with log masking.
5. **Early email verification via a no-payment extend call**, with verify-at-commit as the
   fallback (below). Rejected: verify only at commit time.
6. **Availability unknown or unavailable means no charge and a handoff.** Rejected: rely on
   the extend call (probes showed it does not check availability).
7. **One idempotency key per payment attempt, reused across retries.** An unresolved 5xx on
   a write is reported as "unknown" and never retried with a new key. Probe: two extend
   calls without a key produced two confirmation numbers.
8. **Extensions of 30 minutes or less are advised against** (the grace period covers them,
   and the API bills a full day), but allowed if the customer insists.
9. **Embeddings plus an authority bonus, with a keyword fallback.** A legacy article is
   dropped whenever an official article in the same category ranks above it (or, in the
   keyword fallback, also matches), so a superseded figure such as the legacy 2-hour grace
   period never reaches the model. Rejected: the whole KB in the prompt (loses provenance,
   does not scale) and a vector DB (unneeded at 30 articles).
10. **Model `gpt-5.4-mini`**: low cost and latency, strong tool calling; configurable.
11. **Scripted customers in evals, assertions on the tool trace.** Rejected: an LLM-simulated
    customer (less repeatable) and wording checks (brittle).

### Assumptions

- **Mock API behaviour,** as probed on 2026-10-27: the email is checked before payment;
  extend does not check availability; quotes accept datetimes without an offset. Times
  are the return location's local time.
- **Thresholds:** the $500 auto-charge limit, the 14-day limit and the 30-minute
  "not worth extending" cutoff are my choices, not Roadway policy (all configurable).
- **Grace period:** 30 minutes, from the official extension policy (`kb_ext_01`). The
  legacy "2 hours" article is treated as superseded.
- **Identity:** knowing the reservation ID and the email on file is enough to act, as the
  test accounts in `PROBLEM.md` imply.
- **Payment:** only the card on file is charged. The API requires CVV and billing ZIP on
  every write, and entering them doubles as the customer's authorization.
- **Knowledge base:** an `official-policy` article outranks a `legacy` one on the same
  topic.

### Guardrails (`src/guardrails.py`)

Pure functions; thresholds live in `src/config.py`.

| Check | When | Result |
|---|---|---|
| Already overdue: now is past current return + 30 min grace | pre-quote | handoff `overdue_rental` |
| New return not later than current return | pre-quote | `invalid_request`, agent explains |
| Extension of 30 min or less | pre-quote | `advise_not_needed` unless `customer_insists=true` |
| Extension over 14 days | pre-quote (by date) and post-quote (by `extension_days`) | handoff `extension_too_long` |
| Quote `total_charged` over $500 | post-quote | handoff `high_value` |
| Vehicle type unavailable, or availability unknown after retries | pre-quote | handoff `availability_unconfirmed` |

### Tool outcome vocabulary

Every tool result is a short dict with an `outcome` field the prompt and the evals key off.

| Tool | Outcomes |
|---|---|
| `verify_customer` | `verified`, `not_verified`, `not_eligible`, `system_unavailable`, `escalated` |
| `quote_extension` | `quoted`, `verification_required`, `invalid_datetime`, `invalid_request`, `advise_not_needed`, `quote_failed`, `system_unavailable`, `escalated` |
| `confirm_extension` | `committed`, `no_valid_quote`, `verification_required`, `customer_declined`, `payment_failed`, `not_verified`, `rejected`, `escalated` |
| `search_knowledge_base` | `results` |
| `escalate_to_human` | `escalated` |

### Verify-at-commit fallback

`verify_customer` checks the email without charging: it POSTs an extend with the email and
no payment block. The API checks the email before payment, so a 403 means a wrong email
and a 400 payment-validation error means the email is correct. If the probe fails in any
other way on an active booking, the session is marked `email_unconfirmed` and the flow
continues with only what quoting needs: `verify_customer` returns the return location's
time zone, the current return time and the local time now, but no name, vehicle, card or
membership tier, and the quote and payment prompt say "the card on file" instead of the
card type and last four. `confirm_extension` surfaces a 403 if the email was wrong. That
voids the quote, resets verification and sends the customer back through
`verify_customer`. Apart from those return times and the quoted price, nothing about the
booking, including whether it exists, is disclosed before the email is confirmed; an
unknown reservation looks exactly like a wrong email.

### Handoff deduplication

Handoffs are deduplicated per reason within a session. Opening a second handoff with a
reason already used returns the existing reference with `already_open: true` and writes no
second file. A handoff with a different reason writes a new record.

### Hardening found in review

- Every `verify_customer` call resets verification first, so a failed re-verify cannot
  leave an earlier success standing.
- After a charge with an unknown outcome, quoting and confirming are blocked and the
  existing handoff is returned. The charge is never retried with a new key.
- Every request error from the HTTP client is treated as a transport error.

## Setup

Python 3.10 or higher (the OpenAI Agents SDK requires it).

```bash
python3 --version                  # need 3.10+
python3 -m venv .venv && source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp env.example .env                # then fill in OPENAI_API_KEY and ROADWAY_API_KEY
```

`tzdata` is in `requirements.txt` because `zoneinfo` needs it on Windows.

The mock rental API itself is not part of this repo: the agent talks to it over HTTP as
described in [`docs/api-reference.md`](docs/api-reference.md). The unit tests run against an
in-process fake (`tests/fakes.py`), so they need no network or keys.

## How to run

```bash
python src/agent.py                              # chat with the agent
python src/webui.py   # demo console at http://127.0.0.1:8765 (WEBUI_PORT overrides)
python -m pytest -q                              # unit and fault-injection tests, no network
python -m evals.run_evals --runs 2               # live scenarios (real API and model)
python -m evals.run_evals --scenario overdue     # one scenario
ruff check src tests evals && ruff format --check src tests evals    # lint (ruff.toml)
```

The demo console is a local web page for showing the agent live. The customer chat is on
the left. On the right, an "under the hood" panel shows identity status, the latest quote,
open handoffs and the redacted event trace as the tools run. When the agent asks for
payment, a "Secure payment" card collects CVV and ZIP in the browser and passes them to
the code, never to the model. Five scenario buttons load a prepared reservation and type
the customer's messages: Happy path, Wrong email, Over $500 → human, Overdue → human and
Policy question. Happy path commits a real extension on the mock API. The console uses
the same agent, tools and logs as the CLI.

The chat loop prints a session ID and log path, then reads lines until `exit`, `quit` or
EOF. The CVV and ZIP prompt is masked when stdin is a terminal. Evals and the manual runs
below create real extensions on the mock API.

## Configuration

All settings are optional environment variables (set them in `.env`); defaults shown.

| Variable | Default | Meaning |
|---|---|---|
| `OPENAI_API_KEY`, `ROADWAY_API_KEY`, `ROADWAY_API_URL` | none, none, mock URL | credentials and endpoint |
| `OPENAI_MODEL` | `gpt-5.4-mini` | agent model |
| `EMBEDDING_MODEL` | `text-embedding-3-small` | KB retrieval |
| `MAX_AUTO_CHARGE_USD` | 500 | above this total, hand off |
| `MAX_EXTENSION_DAYS` | 14 | above this, hand off |
| `GRACE_PERIOD_MINUTES` | 30 | overdue threshold |
| `SHORT_EXTENSION_MINUTES` | 30 | at or below this, advise not needed |
| `QUOTE_TTL_MINUTES` | 15 | quote validity |
| `MAX_EMAIL_FAILURES`, `MAX_PAYMENT_FAILURES` | 2, 2 | failures before an automatic handoff |
| `HTTP_CONNECT_TIMEOUT_S`, `HTTP_READ_TIMEOUT_S`, `HTTP_AVAILABILITY_READ_TIMEOUT_S` | 3.0, 10.0, 12.0 | HTTP timeouts |
| `HTTP_MAX_ATTEMPTS`, `HTTP_BACKOFF_BASE_S`, `HTTP_BACKOFF_CAP_S` | 3, 0.5, 4.0 | retry policy (5xx, timeouts, connection errors; never 4xx) |
| `LOG_DIR`, `CACHE_DIR`, `KB_PATH` | `logs`, `.cache`, `data/knowledge-base/articles.json` | paths |

## Logs

Logs are written under `logs/` in the repo root (`LOG_DIR` overrides). The folder is
gitignored. The CLI and the demo console write the same files, and both print the
session ID.

- `logs/sessions/<session_id>.jsonl`: one JSON event per line (`turn_user`, `turn_agent`,
  `tool_call`, `tool_result`, `api_attempt`, `guardrail`, `kb_search`, `handoff`, `error`).
- `logs/handoffs/<session_id>-<n>.json`: handoff record with reference `HO-<session>-<n>`,
  reason code, summary, reservation ID, verified flag, masked email, latest quote,
  idempotency key and charge status, and the last 10 trace events.
- `logs/evals/<timestamp>.json`: results of an eval run.
- `.cache/kb_embeddings.json`: cached KB embeddings keyed by a hash of each article.

Redaction on every event: emails are masked (`s***@example.com`), and any field named
`cvv`, `billing_zip` or `payment` is dropped. In free text (chat turns included), card
numbers, a CVV after "cvv"/"cvc"/"security code" and a ZIP after "zip"/"postal code" are
replaced by `[card]`, `[cvv]` and `[zip]`. API attempts are logged without request
bodies. CVV and ZIP exist only in local variables of `confirm_extension`; they are never
returned, logged or sent to the model.

## Eval results

Two runs against the live API and model: `--runs 2` on 2026-10-27 (**20 of 20 passed**) and
`--runs 1` on 2026-10-28 (**10 of 10 passed**). The unit suite (`python -m pytest -q`): **181
passed**, including the 200-call fault-injection test (30% of attempts failing; no quote
charged twice, no key changed within an attempt, every call ends in `committed`, a handled
failure or an `unknown` handoff).

| Scenario | Reservation | Asserted on the trace | 10-27 run 1 | 10-27 run 2 | 10-28 |
|---|---|---|---|---|---|
| `preferred_happy` | RWR-29471835, +2 days | verified; total $100.49, late fee 0; 1 commit | pass | pass | pass |
| `standard_happy` | RWR-66002030, +2 days | verified; 1 commit | pass | pass | pass |
| `due_soon` | RWR-77001020, +2 days | verified; 1 commit | pass | pass | pass |
| `overdue` | RWR-48372915 | handoff `overdue_rental`; 0 commits | pass | pass | pass |
| `availability_timeout` | RWR-99004050, +2 days | handoff `availability_unconfirmed`; 0 commits | pass | pass | pass |
| `declines_quote` | RWR-50000001, +2 days, customer says no | quote presented; 0 commits | pass | pass | pass |
| `wrong_email_twice` | RWR-66002030 | handoff `verification_failed`; 0 commits | pass | pass | pass |
| `grace_question` | none | KB searched; official article ranked above `kb_fee_01`; answer says 30 minutes, not 2 hours; 0 commits | pass | pass | pass |
| `cancel_request` | RWR-29471835 | KB searched; `escalate_to_human` called; 0 commits | pass | pass | pass |
| `tiny_extension` | RWR-66002030, +20 min | `advise_not_needed`; 0 commits | pass | pass | pass |

`availability_timeout` depends on the mock's OAK/SUV availability timing out; if the mock
answers instead, the scenario is reported as `skipped (precondition)` and a unit test
covers the guardrail. There is no CI workflow in this repo; the commands above are run by
hand.

## Sample transcripts

Real runs of `python src/agent.py` against the live API and model, with piped input.
Piped stdin is not echoed, so the `You:` lines were filled in from the input that was
sent; everything else is the agent's output as printed. Emails appear as typed, since the
CLI does not mask the customer's own input (the logs do). The CVV and ZIP were entered at
the masked prompt (unmasked here because stdin was piped) and are omitted.

The model tended to ask for an exact date when given "two days later at the same time", so
the new return time is given as an explicit date in the first and second runs.

### 1. Happy path (preferred member, RWR-29471835)

```text
Session 863b0fbf; log: logs/sessions/863b0fbf.jsonl
Roadway Assistant: Hi! How can I help with your rental today?
You: I want to extend my rental
Roadway Assistant: Sure — please send your reservation ID and the email on the booking.
You: RWR-29471835, sarah.johnson@example.com
Roadway Assistant: Thanks, Sarah — what new return date and time would you like, in Los Angeles local time?
You: Please return it on June 17, 2027 at 2:00 PM
Roadway Assistant: I can extend it to June 17, 2027 at 2:00 PM Los Angeles time.

Quote:
- 2 extension days
- Subtotal: USD 91.98
- Late fee: USD 0.00
- Taxes and fees: USD 8.51
- Total charged: USD 100.49
- Card: Visa ending in 4832

Reply yes to confirm.
You: yes
Extend RWR-29471835 to 2027-06-17T14:00
Authorize a charge of USD 100.49 to your Visa ending 4832. Press Enter with no input to cancel.
(input is not masked: stdin is not a terminal)
CVV: Billing ZIP: Roadway Assistant: Done — confirmation EXT-294718-695.

New return time: June 17, 2027 at 2:00 PM America/Los_Angeles.
Charged: USD 100.49.
You: quit
```

### 2. Overdue rental (RWR-48372915): refused and handed off

```text
Session 841d2ac3; log: logs/sessions/841d2ac3.jsonl
Roadway Assistant: Hi! How can I help with your rental today?
You: I want to extend my rental
Roadway Assistant: Sure — please send your reservation ID and the email on file.
You: RWR-48372915, marcus.lee@example.com
Roadway Assistant: Thanks, Marcus — what new return date and time would you like at O'Hare International Airport local time?
You: Please return it on June 28, 2026 at 10:00 AM
Roadway Assistant: This extension needs a representative. Reference: HO-841d2ac3-1. A representative will follow up.
You: quit
```

The handoff file `logs/handoffs/841d2ac3-1.json` has reason `overdue_rental` and the masked
email `m***@example.com`.

### 3. Policy question

```text
Session bb834b4f; log: logs/sessions/bb834b4f.jsonl
Roadway Assistant: Hi! How can I help with your rental today?
You: How long is the grace period if I return late?
Roadway Assistant: The grace period is 30 minutes [kb_ext_01].
You: quit
```

## Known limitations

- **The grace-period length still depends on a second search.** Retrieval now drops the
  legacy `kb_fee_01` ("2 hours") whenever an official `fees` article outranks it, so the
  outdated figure no longer reaches the model. But the official articles that rank first
  for "grace period" (`kb_fee_02`, `kb_ext_05`) only refer to "the applicable grace period";
  only `kb_ext_01` states 30 minutes. The prompt makes the agent search again for the
  extension policy when results defer to another policy, and cite only articles that state
  the fact, so stating the length relies on the model following that instruction.
- The scripted manual run depends on the model parsing relative dates ("two days later");
  it sometimes asks for an exact date instead.
- The $500, 14-day and 30-minute thresholds are assumptions, not Roadway policy.
- The email on file is the only identity check.
- Conversation state is in memory; it does not survive a restart.
- Ctrl+C while a charge is being sent exits the CLI with the charge outcome unrecorded:
  no handoff is written and the log may not show whether the card was charged.

## Production next steps

- Replace the terminal CVV/ZIP prompt with a secure payment link or IVR capture, so card
  data never touches the chat channel at all.
- Route handoffs to a real queue (with the transcript context and SLA), instead of files.
- Persist sessions, quotes and idempotency keys in a database, so an unknown charge can be
  reconciled after a restart and quotes survive a process crash.
- Add stronger identity checks (one-time code to the email or phone on file).
- Tune retrieval (reranking, or an eval set of policy questions) rather than relying on
  prompt instructions for the conflicting-article cases.
- Run the evals in CI against a stable sandbox, and monitor tool outcomes, handoff rates
  and API error rates in production.
