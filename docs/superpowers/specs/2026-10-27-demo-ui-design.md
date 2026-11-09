# Demo console (web UI) for the rental-extension agent — design

- **Status:** approved
- **Date:** 2026-10-27

## Purpose

The rental-extension agent (spec `docs/superpowers/specs/2026-10-27-rental-extend-agent-design.md`)
runs only as a terminal chat. The author wants to demo it live with a strong proof of
concept on screen. The point of the demo is
not a polished consumer product: it is to let the viewer **see** that the agent is
safe, that identity is verified before any booking detail is shown, that the quote read out
is exact, that card details never reach the model, and that out-of-policy requests go to a
human. Those rules live in Python inside the tools; the UI makes them visible as they fire.

This spec adds a local web "demo console": the customer chat on the left, a live
"under the hood" panel on the right, and one-click demo scenarios on top. The earlier
spec listed "a web or voice UI" as out of scope for the agent itself; this spec adds a
web UI as a presentation layer on top of the unchanged agent.

## Scope

**In:**

- `src/webui.py` (new): the local web server, the browser payment collector, the demo
  session and the demo scenario list. Started with `python src/webui.py`.
- `src/static/index.html` (new): the single-page console (HTML, CSS and JavaScript in one
  file, no build step, no CDN).
- `src/agent.py`: extract the per-message body of `chat_loop` (run the turn, on an
  exception record the error and build the charge-outcome or error reply) into a function
  both the CLI and the web server call. `chat_loop` behaviour is unchanged.
- `tests/test_webui.py` (new): network-free tests of the web layer.
- `README.md`: how to run the console; the "What was cut" list no longer says there is no
  web UI.
- `.gitignore`: ignore `.superpowers/` (brainstorming mockups).

**Out:**

- Any change to agent behaviour: the instructions, the tools, the guardrails, the
  thresholds, the payment rules and the API client stay as they are.
- Logins or access control, more than one concurrent conversation, mobile layout,
  accessibility beyond sensible defaults, deployment anywhere other than the presenter's
  laptop.
- An offline or recorded "replay" mode: the console needs `OPENAI_API_KEY`,
  `ROADWAY_API_KEY` and network access, like the CLI.
- Presentation slides. A deck may follow as a separate piece of work.
- New npm, Node or Python dependencies.

## Design

### Screen

One page, layout "A" from brainstorming:

- **Header:** "Roadway Assistant" with a thin brand-blue (`#1f5fbf`) accent bar, the session
  ID, and the scenario bar (below).
- **Customer chat (left, about 55% width):** the conversation. Agent bubbles are light
  gray, customer bubbles are dark slate (around `#2f3e4e`) with white text. **Blue is not
  used for chat bubbles**; it appears only as the header accent. A composer at the bottom
  (text box and Send; Enter sends). While a turn runs, a "thinking" indicator shows and
  Send is disabled.
- **Secure payment card (in the chat):** when the agent's `confirm_extension` tool asks
  for card details, a card appears in the chat flow, visually distinct from bubbles,
  titled "Secure payment" with the line "Handled by code, never shown to the AI". It
  restates the amount and card ("Authorize USD 142.30 to your Visa ending 4421", or "the
  card on file" when the collector is given no card), with masked CVV and billing ZIP
  inputs, Authorize and Cancel. Inputs are validated in the browser with the same
  patterns the CLI uses (`CVV_RE`, `ZIP_RE` in `src/payment.py`) and again on the server.
- **Under the hood (right, about 45% width), live:**
  - Three status lines: **Identity** (unverified / verified / email unconfirmed, the
    reservation ID, the email masked with `telemetry.mask_email`), **Quote** (latest
    quote's total and currency, status, expiry time), **Handoffs** (each reason with its
    handoff reference, or "none").
  - A running log of the session trace (`ConversationState.trace`, already redacted), one
    line per event with a local timestamp, newest at the bottom, auto-scrolling. Known
    events get a readable line and an icon or color: `tool_call`, `tool_result` (tool and
    outcome), `guardrail` (check name, verdict and reason; any verdict other than `allow` is
    highlighted), `api_attempt`
    (method, path, status, attempt number), `kb_search` (top article IDs), `handoff`
    (reason, reference), `error`. Chat turn events (`turn_user`, `turn_agent`) are not
    repeated in the log. Any other event shows its name and fields compactly. Clicking a
    line expands its full redacted JSON.

### Demo scenarios

A row of buttons in the header, one per demo scenario, plus **Reset** (starts a fresh
session: new session ID, empty chat and log).

| Button | Source | Expected outcome |
|---|---|---|
| Happy path | `standard_happy` in `evals/scenarios.py` | extension committed, confirmation number shown |
| Wrong email | `wrong_email_twice` | no booking detail shown, handoff |
| Over $500 → human | new, defined in `src/webui.py` | quote refused by the total threshold, handoff (or, for "Long extension", refused by the 14-day rule, `extension_too_long`, before a quote) |
| Overdue → human | `overdue` | handoff for an overdue rental, no charge |
| Policy question | `grace_question` | answer from the knowledge base, no tools on a booking |

Scenarios are reused from `evals.scenarios.SCENARIOS` by name (reservation ID, email,
message templates, `extend_by`, scripted payments), so the demo and the evals stay in
step. The "Over $500" scenario is new: an existing test reservation with an `extend_by`
of at most 14 days whose quote total is above $500, so the total threshold (not the
14-day one) is what refuses it. The builder picks the reservation and duration with a
live quote probe. If no test reservation can exceed $500 within 14 days, the scenario
uses a duration over 14 days and the button reads "Long extension → human". The choice
is fixed in code (no probe at click time). Because the mock's state can drift before the
demo, this scenario's script stops after the new return time: it has no "yes" line and
its scripted payment is `None`, so even if the threshold no longer trips, autoplay cannot
lead to a charge (builder's call).

Filling the templates: `{rid}` and `{email}` come from the scenario; `{new_local}` is
computed with `evals.scenarios.new_return_local` from the reservation, fetched once
through `RoadwayClient` when the scenario is chosen. This read is outside the agent and only
fills the script; it is not shown in the chat or the panel.

Two ways to run a scenario:

- **Live (click the button):** the session resets, the composer gets the first scripted
  line, and a small "Script" cue list under the composer shows every scripted line with
  the values filled in. Clicking a cue puts that line in the composer; the presenter
  edits or sends it. Nothing is sent automatically.
- **Autoplay (the ▶ beside the button):** the session resets and the browser sends each
  scripted line in order, waiting until the previous turn finishes, with about a
  1.5-second pause before each send so the audience can read. When the payment card
  appears, it types the scenario's scripted payment visibly (CVV `847`, ZIP `90210` by
  default) and presses Authorize after a short pause, or presses Cancel when the scripted
  payment is `None`. A Pause/Resume control stops autoplay; typing in the composer or
  clicking a cue also stops it, and the presenter continues by hand.

### Server

- Python standard library only: `http.server.ThreadingHTTPServer` bound to `127.0.0.1`,
  port 8765 by default (`WEBUI_PORT` overrides). On start it prints the URL. It loads
  `.env` and checks the keys the way `agent.main` does.
- One demo session per server process, created with `build_agent` and `build_deps` with
  the web payment collector. Reset replaces it.
- Each customer message runs in a worker thread so the server stays responsive and the
  payment card can appear mid-turn. Only one turn runs at a time; a message sent while a
  turn runs gets HTTP 409.
- The browser polls for state about every 500 ms while a turn runs or a payment is
  pending, and every few seconds otherwise.

Endpoints (JSON):

- `GET /` serves `src/static/index.html`.
- `GET /api/state?since=N`: session ID, chat messages, `busy`, the pending payment request
  (amount, currency, card type, last four, description) or `null`, the status lines'
  data, and the trace events from index `N` on.
- `POST /api/message {text}`: starts a turn.
- `POST /api/payment {cvv, billing_zip}` or `{cancel: true}`: answers the pending payment
  request.
- `POST /api/reset`: new session.
- `GET /api/scenarios` and `POST /api/scenarios/<name>`: the scenario list; choosing one
  resets the session and returns its filled lines and scripted payments.

### Browser payment collector

A `PaymentCollector` (protocol in `src/payment.py`) whose `collect` publishes the payment
request for the browser, then blocks the tool's thread until the browser answers. Card
details go from the request handler to the waiting `collect` call and are returned as
`PaymentDetails`; they are never written to the trace, the chat, the log or
`/api/state`. Cancel, invalid input (after server validation) or no answer within 5
minutes return `None`, which the tools already treat as the customer declining, the same
as empty input in the CLI. A reset while a payment is pending also returns `None`.

### Agent refactor

The per-message work in `chat_loop` (record the quote statuses, call `run_turn`, on an
exception record the error and use `charge_outcome_reply` or `TURN_ERROR_REPLY`, and keep
a reported charge outcome in the history) moves into one function in `src/agent.py`.
`chat_loop` calls it, the web worker calls it, and the existing `tests/test_agent.py`
tests pass unchanged.

## Decisions

1. **Layout A, chat plus a live "under the hood" panel.** Rejected: a customer-only,
   phone-style chat with the trace hidden (looks finished but hides the evidence an
   technical audience cares about). Chosen by the author.
2. **No blue chat bubbles; blue only as the header accent.** The author's call.
3. **Scenario buttons prefill and cue; autoplay is a second, optional mode.** The author
   prefers to type live (looks unscripted) and wants autoplay available.
4. **Standard-library HTTP server and polling.** Rejected: FastAPI or Flask (new
   dependencies for a local demo), WebSockets or server-sent events (more moving parts
   than a 500 ms poll needs). Builder's call, agreed with the author.
5. **One plain HTML file, no framework, no CDN.** Nothing to install or build; the page
   loads even on a flaky network.
6. **The agent is unchanged; only the input side is replaced.** The browser collector
   implements the existing `PaymentCollector` protocol, so every guardrail still runs in
   the tools and the UI cannot bypass one.
7. **Demo scenarios come from `evals/scenarios.py`.** One source for the scripted
   conversations used by the evals and the demo.

## Success criteria

1. **Existing checks still pass:** `python -m pytest -q` passes (all existing tests plus
   the new ones) and `ruff check src tests evals && ruff format --check src tests evals`
   is clean.
2. **Web layer tests, network-free** (`tests/test_webui.py`, run by `python -m pytest -q`):
   - The browser collector returns the submitted `PaymentDetails`, returns `None` on
     cancel, on invalid input and on timeout (with a short test timeout), and a request
     sent while a turn runs gets 409.
   - **Card details stay out of everything:** a test runs a turn whose fake tool call
     collects a payment through the browser collector with CVV `847` and ZIP `90210`;
     afterwards neither string appears in the model history, the session trace, the
     session log file or any `/api/state` response.
3. **Live demo run, manual, in the browser:** with the server running, each of the five
   scenarios is run once by hand (live mode) and once with autoplay, and ends as in the
   scenario table: Happy path shows a confirmation number; Wrong email ends with no
   booking detail shown and a handoff reference; Over $500 (or Long extension) and
   Overdue each end with a handoff reference and no charge; Policy question gets a
   knowledge-base answer. The panel shows the identity, quote and handoff lines and the
   guardrail and API lines for each. The builder runs these with the keys in the repo's
   `.env` (agreed with the author); like the evals, the Happy path runs commit real
   extensions on the shared mock, which is accepted. Evidence: one screenshot per
   scenario sent to the author.
4. **One command starts it:** `python src/webui.py` prints the URL, and the README's "How
   to run" section documents it.
5. **Look:** customer and agent bubbles are slate and gray, not blue. Manual check:
   inspect the screenshots from criterion 3, and in `src/static/index.html` the
   color `#1f5fbf` appears only in the header accent rule.
