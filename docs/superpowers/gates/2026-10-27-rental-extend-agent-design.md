# Gate: Roadway rental-extension agent — design

- **Spec:** docs/superpowers/specs/2026-10-27-rental-extend-agent-design.md
- **Spec SHA-256:** c1e037268da51de7d6dec775eb3c6f51ed7b9896dfffd1c2e11f862b34546b0e
- **Verdict:** pass
- **Date:** 2026-10-27
- **Rounds:** 1 discovery + 0 verification (revision: legacy drop on the embeddings path; earlier: criterion-3 revision 1 + 0, original gate 3 + 1)

## Key decisions

- Support only the Extend workflow end to end; Modify, Cancel and Upgrade get a KB policy answer and a handoff (Purpose, Scope Out, Decision 1)
- Every money and identity rule is enforced in Python inside the tools, not in the prompt (Design/Shape, Decision 2)
- Escalation thresholds: total > $500, extension > 14 days, and overdue past the 30-minute grace always escalates; configurable assumptions, not Roadway policy (Guardrails, Decision 3)
- CVV and ZIP are collected by a masked prompt outside the model, which is also the authorization step; card on file only (Payment collection, Scope Out, Decision 4)
- Early email verification through a no-payment `extend` call, relying on mock behaviour observed on 2026-10-27; verify-at-commit as the fallback; nothing about the booking (including existence or status) is disclosed before verification (verify_customer, Decision 5, Assumptions)
- Unavailable or unknown availability means no charge and a handoff (Guardrails, Decision 6)
- One idempotency key per payment attempt, reused across retries; an unresolved 5xx/timeout on a charge is reported as unknown, handed off and never retried with a new key (confirm_extension, Decision 7)
- Extensions of 30 minutes or less are advised against but allowed if the customer insists (Guardrails, Decision 8)
- KB retrieval uses OpenAI embeddings plus an authority bonus, cached locally, with a keyword fallback that drops legacy articles when a same-category official article matches; with embeddings, a legacy article is dropped when a same-category official article ranks above it and the next result fills its slot; official policy overrides legacy (Retrieval, Decision 9; revised 2026-10-27 at the user's request)
- Default model `gpt-5.4-mini`, configurable (Agent and CLI, Decision 10)
- Evals use scripted customers against the live API and model, asserting on the tool trace; every scenario must pass in both runs of one `--runs 2` invocation (Evals, Decision 11, Success criterion 3)
- Handoffs are written to `logs/handoffs/` rather than routed; no state persistence across conversations; the email on file is the only identity; no web or voice UI (Scope Out, Handoffs, Assumptions)
- `grace_question` passes when the top result of the first `kb_search` is an official-policy article, ranked above legacy `kb_fee_01`, not when `kb_ext_01` ranks first; the answer's 30 minutes may come from the agent instructions (Success criterion 3, revised 2026-10-27 after live runs ranked `kb_fee_02`/`kb_ext_05` first, user's choice)

## Findings fixed

- revision round 1 [minor] Evals prose vs scenario table: the prose now says the answer matches `30[- ]minute` and does not contain "2 hours".
- revision round 1 [minor] "only `kb_ext_01` states" was imprecise: it now says only `kb_ext_01` (official) states the length correctly, and legacy `kb_fee_01` gives 2 hours.
- retrieval revision round 1 [minor] Decision 9 did not mention the legacy-drop rule: Decision 9 now states it on both paths and the rejected alternative (bonus alone).
- retrieval revision round 1 [minor] Dated revision note inside the Retrieval bullet: moved to Decision 9.

- round 1 [blocking] Guardrail result set omits `invalid`: the result set now includes `invalid(<reason>)`, mapped to `outcome: invalid_request` with no handoff and no quote call.
- round 1 [minor] "two consecutive runs" wording: criterion 3 now says one `--runs 2` invocation in which every scenario passes (or is skipped on its precondition) in both runs.
- round 2 [blocking] KB ranking test cannot run offline: criterion 1 split into an offline test (stubbed embeddings, authority ordering, keyword fallback) and a live ranking check in the `grace_question` scenario; the keyword fallback drops a legacy article when a same-category official article matches.
- round 2 [blocking] `verify_customer` disclosed status before verification: a 404 now returns `not_verified` (same as a wrong email); a non-active booking is reported as `not_eligible` only after the probe, without describing the booking; `verified` outcome named.
- round 2 [minor] Fallback zone is not DST-correct: criterion 1 says DST-correctness applies to mapped codes only; the fallback is a fixed offset.
- round 2 [minor] `grace_question` wording check: the assertion is now the regex `30[- ]minute` plus `kb_ext_01` ranked above `kb_fee_01` in the `kb_search` results.
- round 3 [blocking] KB fallback test named an official article from a different category: criterion 1 now says `kb_fee_01` is excluded because `kb_fee_02` (official, `fees`) matches, and `kb_ext_01` is among the results.
- round 3 [minor] `INVALID_CHANGE` undocumented: the guardrail table marks it "observed on probe; undocumented".

## Plan questions

- Should the equal-similarity authority test keep the three articles in separate categories so the legacy drop does not hide the legacy article? (Success criterion 1, kb) — answered in `tests/test_kb.py`: yes, categories x/y/z.

- What is the full `outcome` vocabulary per tool, including the first wrong email, `advise_not_needed` (and whether availability/quote calls are skipped for it), and `extension_too_long`/`high_value` escalations, so the prompt and eval assertions agree? (Tools, Guardrails)
- After a verify-at-commit 403, what are the quote status and verification state, and which tool does the customer go through next? (verify_customer, confirm_extension)
- Should `verify_customer` also apply the overdue check, so an overdue customer is told before giving a date, or does the `overdue` eval script supply a date? (Guardrails, Evals)
- Are the `/availability` `start_date`/`end_date` local dates at the return location, given `current_return_datetime` can arrive in UTC? (quote_extension)
- Does the KB ranking test use stubbed embeddings, and should the embeddings path also drop or more heavily penalise legacy articles if the live `grace_question` ranking proves close? (Retrieval, Success criteria)
- Does the fault-injection fake simulate server-side commit before a client-side failure, with idempotency-key replay? (Success criterion 2)
- In `declines_quote`, does the customer decline in chat or at the scripted payment prompt? (Evals)
- Do the tools dedupe or refuse a second handoff for the same reason in a session (guardrail auto-handoff followed by the model calling `escalate_to_human`)? (Handoffs)
- Is email masking regex-based over all string values (including `turn_user`/`turn_agent`), and should CVV/ZIP-like digit runs typed into chat be scrubbed? Should the agent tell customers not to type card details in chat? (Logging)
- How do `evals` (run with `python -m` from the repo root) import the flat-import `src` modules? (Agent, Evals)
- How is `skipped (precondition)` detected for `availability_timeout` (for example from `api_attempt` events on `/availability`)? (Evals)
- Which env var names does `src/config.py` expose for thresholds, retries and timeouts? (Config)
- How is the pre-quote 14-day check measured (elapsed time vs billable days), and what is the boundary test? (Guardrails)
- Is `POST /quote` retried like a read (side-effect free) or treated as a write needing an idempotency key? (API client)
- What outcomes do a GET reservation 5xx in `verify_customer`, and quote-endpoint 4xx/5xx in `quote_extension`, return, and do any create a handoff? (Tools)
- What does the `kb_search` event log (ordered IDs, scores, fallback flag)? (Logging)
- How are local times in a DST gap or overlap handled (reject as `invalid_datetime` or a fixed `fold` rule)? (Time zones)
- Should the agent verify the customer before an out-of-scope handoff, or hand off with only the ID? (Flow step 1, Handoffs)

## Open

- none
