# Gate: Demo console (web UI) for the rental-extension agent — design

- **Spec:** docs/superpowers/specs/2026-10-27-demo-ui-design.md
- **Spec SHA-256:** 80d706e17f9d64241f9042698cfa0478bf2a818589d8a3d95211edbd0b0089ce
- **Verdict:** pass
- **Date:** 2026-10-27
- **Rounds:** 1 discovery + 0 verification

## Key decisions

- The web UI is a presentation layer only: agent instructions, tools, guardrails, thresholds, payment rules and API client are unchanged; the input side gets a new `PaymentCollector` implementation.
- The earlier spec's "no web UI" is reversed for a local demo console only: one session, `127.0.0.1`, no auth.
- Layout A: customer chat plus a live "under the hood" panel showing the redacted trace.
- Blue (`#1f5fbf`) only as the header accent, never for chat bubbles.
- Scenario buttons prefill and show cues by default; autoplay is a second, optional mode.
- Standard-library `ThreadingHTTPServer` with 500 ms polling; no new dependencies.
- One HTML file, no framework, no build step, no CDN.
- Demo scenarios reused from `evals/scenarios.py`; one new fixed "Over $500" scenario whose script has no "yes" line and a `None` payment, so autoplay cannot charge.
- Card details reach only the waiting `collect` call; cancel, invalid input, a 5-minute timeout or a reset count as a decline.
- The `chat_loop` per-message logic moves into a shared function; CLI behaviour unchanged.
- Manual verification uses the real `.env` keys; Happy-path runs commit real extensions on the shared mock, which is accepted.
- No offline or replay mode.

## Findings fixed

- round 1 [minor] Expected outcome for the fallback scenario: the table row now names the 14-day refusal (`extension_too_long`) for "Long extension".
- round 1 [minor] Guardrail log wording: the panel shows check name, verdict and reason; any verdict other than `allow` is highlighted.
- round 1 [minor] Scope of criterion 5's color check: `#1f5fbf` may appear only in the header accent rule.

## Plan questions

- During a running turn (no payment pending), is Reset or a scenario choice refused (409) or allowed, and if allowed how is the old worker's output kept out of the new session? (Design > Server; Browser payment collector)
- How does `python src/webui.py` make the repo root importable for `evals.scenarios`? (Scope; Demo scenarios)
- Does `Runner.run_sync` (openai-agents 0.24) work in a non-main worker thread, or does the worker need its own event loop? (Design > Server)
- What injection point does the web session expose for a fake runner and fake deps so tests stay network-free? (Success criteria 2)
- What is the exact `/api/state` JSON shape (chat entries, status-line data including quote expiry from `created_at` + `quote_ttl_minutes`, whether the payment card is a chat item)? (Design > Endpoints)
- Does an answered payment card stay in the chat? (Design > Screen)
- What does the extracted per-message function return, and do empty-text and exit-word handling stay in `chat_loop`? (Design > Agent refactor)

## Open

- none
