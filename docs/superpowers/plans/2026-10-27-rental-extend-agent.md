# Roadway rental-extension agent Implementation Plan

- **Status:** approved
- **Date:** 2026-10-27

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A CLI agent (OpenAI Agents SDK) that fully handles extending an active Roadway rental against the mock Roadway API, answers servicing policy questions from the knowledge base, and hands every other request to a human with a structured handoff record.

**Architecture:** One SDK agent owns the conversation; every money and identity rule lives in Python inside five function tools (`verify_customer`, `quote_extension`, `confirm_extension`, `search_knowledge_base`, `escalate_to_human`). Each tool is a plain, testable `*_impl(deps, ...)` function in `src/tools.py` plus a thin `@function_tool` wrapper that reads a `ToolDeps` object from the SDK run context. Supporting modules are small and single-purpose: resilient HTTP client, pure guardrails, time zones, per-conversation state, handoff writer, redacting JSON-lines logger, KB retrieval, payment collection outside the model.

**Tech Stack:** Python 3.13 (3.10+ works), `openai-agents` 0.24, `openai`, `requests`, `python-dotenv`, `tzdata`, `pytest`.

**Spec:** `docs/superpowers/specs/2026-10-27-rental-extend-agent-design.md`

## Context for a cold executor

- Read the spec above and `docs/api-reference.md` before starting. `PROBLEM.md` lists the six test reservations and their emails (Test accounts table).
- Source modules use **flat imports** (`from config import Settings`, as the existing `src/agent.py` does with `from roadway_client import ...`) because `python src/agent.py` puts `src/` on `sys.path`. Tests get `src/` and the repo root on the path from `pytest.ini` (Task 1). `evals/__init__.py` inserts `src/` into `sys.path` (Task 12), so `python -m evals.run_evals` works from the repo root.
- `.env` (gitignored) holds `OPENAI_API_KEY`, `ROADWAY_API_KEY`, `ROADWAY_API_URL`. In a worktree under `<repo-root>/.claude/worktrees/`, `load_dotenv()` searches upward and finds `<repo-root>/.env`; nothing to copy. Only Tasks 12 and 13 need it (live API and model). Unit tests never touch the network.
- The SDK has its own `Session` class; this plan's per-conversation state class is therefore named `ConversationState`.
- Decisions already made by the spec (do not relitigate): Extend only; guardrails in code; thresholds $500 / 14 days / overdue past 30-minute grace; masked payment prompt outside the model; early email check via a no-payment `extend` probe with verify-at-commit fallback; unknown availability means hand off; one idempotency key per payment attempt; unknown charge outcome is never retried; embeddings plus authority bonus with keyword fallback; model `gpt-5.4-mini`; scripted eval customers asserting on the trace.

## Global Constraints

- Thresholds (env-overridable, `src/config.py`): `MAX_AUTO_CHARGE_USD=500`, `MAX_EXTENSION_DAYS=14`, `GRACE_PERIOD_MINUTES=30`, `SHORT_EXTENSION_MINUTES=30`, `QUOTE_TTL_MINUTES=15`, `MAX_EMAIL_FAILURES=2`, `MAX_PAYMENT_FAILURES=2`.
- HTTP: connect timeout 3 s; read timeout 10 s (12 s for `/availability`); up to 3 attempts on 5xx, timeouts and connection errors; backoff 0.5 s then 1 s, cap 4 s, with jitter; no retry on 4xx; writes retried only with an `Idempotency-Key`, reusing it.
- Idempotency keys: probe `verify-<session_id>-<attempt>`; charge `ext-<session_id>-<quote_token>-<attempt>`.
- Datetimes sent to the API always carry an explicit UTC offset.
- CVV and billing ZIP are never returned by a tool, logged, written to a handoff or put in model context. Log redaction: emails masked as `d***@example.com`; keys `cvv`, `billing_zip`, `payment` dropped at any depth.
- Logs: `logs/sessions/<session_id>.jsonl`, `logs/handoffs/<session_id>-<n>.json`, `logs/evals/<timestamp>.json`. Cache: `.cache/kb_embeddings.json`. `logs/` and `.cache/` are gitignored.
- Model default `gpt-5.4-mini` (`OPENAI_MODEL`); embeddings `text-embedding-3-small` (`EMBEDDING_MODEL`).
- Authority bonus: `official-policy` +0.05, `help-center` 0, `legacy` −0.05.
- Unit tests make no network calls (no `OPENAI_API_KEY` or `ROADWAY_API_KEY` needed).

## Tool outcome vocabulary (answers the spec gate's first plan question)

Every tool result is a dict with `outcome` plus the fields named. The prompt (Task 11) and eval checks (Task 12) use exactly these strings.

| Tool | Outcomes |
|---|---|
| `verify_customer` | `verified` (summary; `email_confirmed: bool`), `not_verified` (`attempts_remaining`; used for a wrong email **and** for a 404, and for the first failure), `not_eligible`, `system_unavailable`, `escalated` (`reason` one of `verification_failed`, `unexpected_charge_on_verification`; `handoff_reference`, `message`) |
| `quote_extension` | `quoted`, `verification_required`, `invalid_datetime` (`hint`), `invalid_request` (`reason`, `message`), `advise_not_needed` (`message`; **no** availability or quote call made), `quote_failed` (`code`, `message`), `system_unavailable`, `escalated` (`reason` one of `overdue_rental`, `extension_too_long`, `high_value`, `availability_unconfirmed`; `handoff_reference`; `alternatives` for availability) |
| `confirm_extension` | `committed`, `no_valid_quote` (`reason`: `no_quote`, `superseded`, `not_presented`, `expired`), `verification_required`, `customer_declined`, `payment_failed` (`attempts_remaining`), `not_verified` (verify-at-commit 403, first time), `rejected` (`code`, `message`), `escalated` (`reason` one of `payment_failed`, `verification_failed`, `charge_status_unknown`) |
| `search_knowledge_base` | `results` (`articles`, `fallback: bool`) |
| `escalate_to_human` | `escalated` (`handoff_reference`, `already_open: bool`) |

Every `escalated` result means a handoff record exists. Handoffs are **deduplicated per reason within a session**: opening a second handoff with a reason already used returns the existing reference with `already_open: true` and writes no file. `escalate_to_human` called after an automatic handoff with a different reason writes a new record.

## Review Focus

- A date typed in natural language or the wrong format (`June 17 at 2pm`, `2027-06-17 14:00`) must give `invalid_datetime` with a hint naming the zone and the `YYYY-MM-DDTHH:MM` format, never an API call (test `test_quote_rejects_bad_format`, Task 9).
- A reservation ID typed in lower case with spaces and an email in mixed case must verify normally (test `test_verify_normalises_inputs`, Task 9).
- The customer says yes twice and the model calls `confirm_extension` twice with the same token: the second call must return `no_valid_quote` and make no API call (test `test_confirm_twice_charges_once`, Task 10).
- The API returns a 502 with an HTML body: the client must raise `RoadwayAPIError(status=502, code="HTTP_502", retryable=True)`, not crash on JSON decoding (test `test_non_json_error_body`, Task 4).
- A reservation whose return location code is unmapped and whose `current_return_datetime` arrives in UTC: the zone must come from the pickup offset and the verified summary must show local time in that offset (test `test_verify_unmapped_location_uses_pickup_offset`, Task 9).

---

### Task 1: Project setup and settings

**Files:**
- Modify: `requirements.txt`
- Modify: `.gitignore`
- Modify: `env.example`
- Create: `pytest.ini`
- Create: `src/config.py`
- Test: `tests/test_config.py`

**Depends on:** none

**Interfaces:**
- Produces: `Settings` (frozen dataclass) and `load_settings(env: Mapping[str, str] | None = None) -> Settings` in `src/config.py`. Field names and defaults (env var = upper-case field name):
  `max_auto_charge_usd: float = 500.0`, `max_extension_days: int = 14`, `grace_period_minutes: int = 30`, `short_extension_minutes: int = 30`, `quote_ttl_minutes: int = 15`, `max_email_failures: int = 2`, `max_payment_failures: int = 2`, `http_connect_timeout_s: float = 3.0`, `http_read_timeout_s: float = 10.0`, `http_availability_read_timeout_s: float = 12.0`, `http_max_attempts: int = 3`, `http_backoff_base_s: float = 0.5`, `http_backoff_cap_s: float = 4.0`, `openai_model: str = "gpt-5.4-mini"`, `embedding_model: str = "text-embedding-3-small"`, `log_dir: str = "logs"`, `cache_dir: str = ".cache"`, `kb_path: str = "data/knowledge-base/articles.json"`, `roadway_api_url: str = ""`, `roadway_api_key: str = ""`. `load_settings(None)` reads `os.environ` (after `load_dotenv()`); relative `log_dir`, `cache_dir`, `kb_path` resolve against the repo root (parent of `src/`).

- [ ] **Step 1: Update project files**
  - `requirements.txt`: keep the existing lines; add `tzdata>=2024.1` and `pytest>=8`. Remove the commented RAG suggestions block.
  - `.gitignore`: add `logs/` and `.cache/`.
  - `env.example`: append a commented "Optional settings" block listing every env var above with its default (e.g. `# MAX_AUTO_CHARGE_USD=500`), plus `# OPENAI_MODEL=gpt-5.4-mini`.
  - Then `pip install -r requirements.txt`.
  - `pytest.ini` (test plumbing for the spec's `tests/` scope item):
    ```ini
    [pytest]
    pythonpath = src .
    testpaths = tests
    ```
- [ ] **Step 2: Write the failing tests** in `tests/test_config.py`

```python
def test_defaults():
    s = load_settings({})
    assert s.max_auto_charge_usd == 500.0 and s.max_extension_days == 14
    assert s.quote_ttl_minutes == 15 and s.openai_model == "gpt-5.4-mini"
    assert s.http_max_attempts == 3 and s.http_backoff_base_s == 0.5

def test_env_override_and_types():
    s = load_settings({"MAX_AUTO_CHARGE_USD": "750", "MAX_EXTENSION_DAYS": "7"})
    assert s.max_auto_charge_usd == 750.0 and s.max_extension_days == 7

def test_paths_resolve_to_repo_root():
    s = load_settings({})
    assert Path(s.kb_path).is_file()
```

- [ ] **Step 3: Run to verify they fail**
  Run: `python -m pytest tests/test_config.py -v`
  Expected: FAIL with `ModuleNotFoundError: No module named 'config'`
- [ ] **Step 4: Implement** `src/config.py` per the Interfaces block (cast each env string to the field's type).
- [ ] **Step 5: Run to verify they pass**
  Run: `python -m pytest tests/test_config.py -v`
  Expected: 3 passed
- [ ] **Step 6: Commit**
  ```bash
  git add requirements.txt .gitignore env.example pytest.ini src/config.py tests/test_config.py
  git commit -m "feat: add settings module and test setup"
  ```

### Task 2: Time zones

**Files:**
- Create: `src/timeutil.py`
- Test: `tests/test_timeutil.py`

**Depends on:** none

**Interfaces:**
- Produces in `src/timeutil.py`:
  - `LOCATION_ZONES: dict[str, str]` — LAX SFO OAK SNA SAN SJC SMF BUR ONT SEA PDX LAS → `America/Los_Angeles`; PHX → `America/Phoenix`; DEN SLC → `America/Denver`; DFW IAH HOU AUS SAT MSY ORD MDW MSP STL → `America/Chicago`; DTW → `America/Detroit`; ATL MIA MCO TPA FLL CLT JFK LGA EWR BOS IAD DCA BWI PHL → `America/New_York`; ANC → `America/Anchorage`; HNL → `Pacific/Honolulu`.
  - `class InvalidLocalTime(ValueError)` with attribute `hint: str`.
  - `parse_api_datetime(value: str) -> datetime` — aware datetime; accepts `Z` and `±HH:MM`; a naive value is treated as UTC.
  - `zone_for(location_code: str, pickup_datetime: str) -> tzinfo` — `ZoneInfo` for mapped codes (case-insensitive); otherwise `timezone(<pickup offset>)`, a fixed offset.
  - `zone_label(tz: tzinfo) -> str` — the IANA key for a `ZoneInfo`, else `UTC±HH:MM`.
  - `parse_local(value: str, tz: tzinfo) -> datetime` — accepts only `YYYY-MM-DDTHH:MM` (regex `^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$`); returns aware datetime in `tz`. **DST gap** (non-existent wall time) → `InvalidLocalTime`; **DST overlap** → `fold=0` (the earlier instant). Detect a gap by round-tripping through UTC and comparing wall time. Hint text: `Use YYYY-MM-DDTHH:MM in <zone_label> local time, e.g. 2027-06-17T14:00`.
  - `to_api(dt: datetime, tz: tzinfo) -> str` — `dt.astimezone(tz).isoformat(timespec="seconds")`, always with offset.
  - `format_local(dt: datetime, tz: tzinfo) -> str` — `YYYY-MM-DDTHH:MM` in `tz`.

- [ ] **Step 1: Write the failing tests** in `tests/test_timeutil.py`

```python
def test_known_code_dst_correct():
    tz = zone_for("lax", "2027-06-12T10:00:00-07:00")
    assert to_api(parse_local("2027-01-15T10:00", tz), tz).endswith("-08:00")
    assert to_api(parse_local("2027-07-15T10:00", tz), tz).endswith("-07:00")

def test_unknown_code_uses_pickup_offset():
    tz = zone_for("ZZZ", "2027-06-12T10:00:00-05:00")
    assert parse_local("2027-12-01T09:00", tz).utcoffset() == timedelta(hours=-5)
    assert zone_label(tz) == "UTC-05:00"

def test_utc_input_converted():
    tz = zone_for("LAX", "2027-06-12T10:00:00-07:00")
    assert format_local(parse_api_datetime("2027-06-15T21:00:00Z"), tz) == "2027-06-15T14:00"

def test_rejects_other_formats():
    for bad in ["2027-06-17 14:00", "June 17 2pm", "2027-06-17T14:00:00-07:00", ""]:
        with pytest.raises(InvalidLocalTime): parse_local(bad, ZoneInfo("America/Los_Angeles"))

def test_dst_gap_rejected_and_overlap_takes_earlier():
    tz = ZoneInfo("America/Los_Angeles")
    with pytest.raises(InvalidLocalTime): parse_local("2027-03-14T02:30", tz)
    assert parse_local("2027-11-07T01:30", tz).utcoffset() == timedelta(hours=-7)
```

- [ ] **Step 2: Run to verify they fail**
  Run: `python -m pytest tests/test_timeutil.py -v`
  Expected: FAIL with `ModuleNotFoundError: No module named 'timeutil'`
- [ ] **Step 3: Implement `src/timeutil.py`** per the Interfaces block.
- [ ] **Step 4: Run to verify they pass**
  Run: `python -m pytest tests/test_timeutil.py -v`
  Expected: 5 passed
- [ ] **Step 5: Commit**
  ```bash
  git add src/timeutil.py tests/test_timeutil.py
  git commit -m "feat: add location time zones and local datetime parsing"
  ```

### Task 3: Telemetry with redaction

**Files:**
- Create: `src/telemetry.py`
- Test: `tests/test_telemetry.py`

**Depends on:** none

**Interfaces:**
- Produces in `src/telemetry.py`:
  - `mask_email(email: str) -> str` — first character of the local part + `***@` + domain (`david.kim@example.com` → `d***@example.com`).
  - `redact(value: Any) -> Any` — recursive copy: dict keys `cvv`, `billing_zip`, `payment` (case-insensitive) dropped at any depth; every string (dict values, list items, including free text such as `turn_user`/`turn_agent`) has each email substring, matched by regex `[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}`, replaced by `mask_email(match)`. No digit-run scrubbing (it would mangle dates, times and amounts; card details are never collected in chat — Task 11 instructions tell customers not to type them).
  - `class SessionLogger(session_id: str, log_dir: Path)` with `path: Path` (`<log_dir>/sessions/<session_id>.jsonl`, parent created) and `write(event: str, level: str = "info", **fields) -> dict` — builds `{"ts": <UTC ISO>, "session_id", "event", "level", **fields}`, applies `redact`, appends one JSON line (`default=str`), returns the redacted record.
  - `EVENTS = {"turn_user", "turn_agent", "tool_call", "tool_result", "api_attempt", "guardrail", "kb_search", "handoff", "error"}`; `write` raises `ValueError` for any other event name.

- [ ] **Step 1: Write the failing tests** in `tests/test_telemetry.py`

```python
def test_mask_email():
    assert mask_email("david.kim@example.com") == "d***@example.com"

def test_redact_drops_payment_keys_at_any_depth():
    out = redact({"a": {"payment": {"cvv": "847"}, "cvv": "1", "billing_zip": "90210", "ok": 1}})
    assert out == {"a": {"ok": 1}}

def test_redact_masks_emails_in_free_text():
    assert redact({"text": "I'm Sarah.Johnson@example.com"})["text"] == "I'm S***@example.com"

def test_logger_writes_redacted_jsonl(tmp_path):
    log = SessionLogger("abc", tmp_path)
    log.write("tool_call", args={"email": "x.y@example.com", "cvv": "9731"})
    rec = json.loads(log.path.read_text().strip())
    assert rec["event"] == "tool_call" and rec["args"] == {"email": "x***@example.com"}

def test_unknown_event_rejected(tmp_path):
    with pytest.raises(ValueError): SessionLogger("abc", tmp_path).write("nope")
```

- [ ] **Step 2: Run to verify they fail**
  Run: `python -m pytest tests/test_telemetry.py -v`
  Expected: FAIL with `ModuleNotFoundError: No module named 'telemetry'`
- [ ] **Step 3: Implement `src/telemetry.py`** per the Interfaces block.
- [ ] **Step 4: Run to verify they pass**
  Run: `python -m pytest tests/test_telemetry.py -v`
  Expected: 5 passed
- [ ] **Step 5: Commit**
  ```bash
  git add src/telemetry.py tests/test_telemetry.py
  git commit -m "feat: add redacting JSON-lines session logger"
  ```

### Task 4: Resilient Roadway API client

**Files:**
- Modify: `src/roadway_client.py`
- Create: `tests/fakes.py`
- Test: `tests/test_roadway_client.py`

**Depends on:** Task 1

**Interfaces:**
- Consumes: `Settings` (Task 1).
- Produces in `src/roadway_client.py` (the module no longer reads env vars at import; the `get_reservation` module function is removed):
  - `class RoadwayAPIError(Exception)` with attributes `status: int | None`, `code: str`, `message: str`, `details: dict`, `retryable: bool`. Error envelope `{"error": {"code","message","details"}}` is parsed; a non-JSON or envelope-less body gives `code=f"HTTP_{status}"`. Transport failure after retries: `status=None, code="TRANSPORT_ERROR", retryable=True`. `retryable` is true for 5xx and transport errors.
  - `class RoadwayClient(base_url: str, api_key: str, settings: Settings, http: requests.Session | None = None, on_attempt: Callable[[dict], None] | None = None, sleep: Callable[[float], None] = time.sleep, rng: random.Random | None = None)`; `RoadwayClient.from_settings(settings, **kw)` uses `settings.roadway_api_url`/`roadway_api_key`.
  - Methods: `get_reservation(reservation_id: str) -> dict`; `get_availability(location: str, vehicle_type: str, start_date: str, end_date: str) -> dict`; `quote_extension(reservation_id: str, new_return_datetime: str) -> dict` (POST `/quote` body `{"change_type": "extend", "new_return_datetime": ...}`; **treated as side-effect free and retried like a read, no idempotency key**); `extend(reservation_id: str, *, email: str, new_return_datetime: str, payment: dict | None, idempotency_key: str | None) -> dict` (omits `payment` from the body when `None`).
  - Internal `_request(method, path, *, params=None, json=None, idempotency_key=None, safe=False, read_timeout=None) -> dict`: headers `X-API-Key`, `Content-Type: application/json`, and `Idempotency-Key` when given; timeout `(connect, read)`; retry when `method == "GET" or safe or idempotency_key` and the failure is 5xx, `requests.Timeout` or `requests.ConnectionError`, up to `http_max_attempts`. Delay before attempt n+1: `min(cap, base * 2**(n-1)) * rng.uniform(0.8, 1.2)`, then `min(cap, ...)` again. `on_attempt` receives `{"method","path","status","error_code","latency_ms","attempt","idempotency_key"}` for every attempt (never body, email or payment); default `on_attempt` is a no-op.
- Produces in `tests/fakes.py`: `FakeResponse(status_code: int, body: dict | str)` with `.json()` (raises `ValueError` for a str body) and `.text`; `ScriptedHTTP(script: list[FakeResponse | Exception])` implementing `request(method, url, *, headers, params=None, json=None, timeout=None)`, popping the next item (raising it if it is an exception) and recording each call in `.calls` as a dict of those arguments.

- [ ] **Step 1: Write the failing tests** in `tests/test_roadway_client.py` (client built with `sleep=sleeps.append`, `rng=random.Random(0)`, `ScriptedHTTP`)

```python
def test_retries_5xx_then_succeeds(): # 503, 503, 200 -> returns body; 3 calls; sleeps ~[0.5, 1.0] within ±20%
def test_retries_timeout(): # requests.Timeout, 200 -> success; 2 calls
def test_no_retry_on_4xx(): # 404 envelope RESERVATION_NOT_FOUND -> RoadwayAPIError(status=404, code="RESERVATION_NOT_FOUND", retryable=False); 1 call
def test_gives_up_after_three(): # 500 x3 -> RoadwayAPIError(status=500, retryable=True); 3 calls
def test_transport_error_after_retries(): # ConnectionError x3 -> status None, code "TRANSPORT_ERROR"
def test_write_without_key_not_retried(): # extend(idempotency_key=None) with 503 -> raises after 1 call
def test_write_with_key_retried_with_same_key(): # extend(idempotency_key="k1"), 503 then 200 -> 2 calls, both headers["Idempotency-Key"] == "k1"
def test_quote_retried_without_key(): # quote_extension 503, 200 -> 2 calls, no Idempotency-Key header
def test_availability_uses_longer_read_timeout(): # calls[0]["timeout"] == (3.0, 12.0)
def test_error_fields(): # 402 PAYMENT_DECLINED with details -> message and details copied
def test_non_json_error_body(): # FakeResponse(502, "<html>") x3 -> status 502, code "HTTP_502", retryable True
def test_on_attempt_never_sees_email_or_payment(): # extend with email+payment; every recorded attempt dict has no "email"/"cvv" in str(dict)
```

  Each test asserts exactly what its comment states.
- [ ] **Step 2: Run to verify they fail**
  Run: `python -m pytest tests/test_roadway_client.py -v`
  Expected: FAIL (ImportError on `RoadwayClient` / `RoadwayAPIError`)
- [ ] **Step 3: Rewrite `src/roadway_client.py`** per the Interfaces block. Keep a `__main__` block that loads `.env`, builds `RoadwayClient.from_settings(load_settings())`, and prints the sample reservation as the old one did.
- [ ] **Step 4: Run to verify they pass**
  Run: `python -m pytest tests/test_roadway_client.py -v`
  Expected: 12 passed
- [ ] **Step 5: Commit**
  ```bash
  git add src/roadway_client.py tests/fakes.py tests/test_roadway_client.py
  git commit -m "feat: resilient Roadway API client with retries and idempotency"
  ```

### Task 5: Guardrails

**Files:**
- Create: `src/guardrails.py`
- Test: `tests/test_guardrails.py`

**Depends on:** Task 1

**Interfaces:**
- Consumes: `Settings` (Task 1).
- Produces in `src/guardrails.py`:
  - `@dataclass(frozen=True) class Verdict: kind: str; reason: str | None = None` where `kind` ∈ `allow`, `invalid`, `advise_not_needed`, `escalate`; `ALLOW = Verdict("allow")`.
  - `pre_quote(current_return: datetime, new_return: datetime, now: datetime, settings: Settings, customer_insists: bool = False) -> Verdict`. Checks in this order, first hit wins:
    1. `now > current_return + grace_period_minutes` → `escalate("overdue_rental")` (exactly at +30 min is allowed).
    2. `new_return <= current_return` → `invalid("not_later_than_current_return")`.
    3. `new_return - current_return <= short_extension_minutes` and not `customer_insists` → `advise_not_needed`.
    4. `new_return - current_return > timedelta(days=max_extension_days)` (elapsed time, not billable days) → `escalate("extension_too_long")`. Exactly 14 days 0 minutes is allowed; 14 days 1 minute escalates.
  - `post_quote(charges: dict, settings: Settings) -> Verdict`: `charges["extension_days"] > max_extension_days` → `escalate("extension_too_long")`; else `charges["total_charged"] > max_auto_charge_usd` → `escalate("high_value")`; else `ALLOW`. Exactly 500.00 is allowed.
  - `availability_verdict(availability: dict | None) -> Verdict`: `None` (unknown) or `availability["availability"]["requested_type"]["available"]` falsy → `escalate("availability_unconfirmed")`; else `ALLOW`.

- [ ] **Step 1: Write the failing tests** in `tests/test_guardrails.py`, with `CUR = 2027-06-15T14:00-07:00`, `NOW = 2027-06-14T12:00-07:00`, `S = load_settings({})`:

```python
def test_overdue_boundary():
    assert pre_quote(CUR, CUR + timedelta(days=2), CUR + timedelta(minutes=30), S) == ALLOW
    assert pre_quote(CUR, CUR + timedelta(days=2), CUR + timedelta(minutes=31), S) == Verdict("escalate", "overdue_rental")

def test_not_later_is_invalid():  # new == CUR and new == CUR - 1h -> Verdict("invalid", "not_later_than_current_return")
def test_short_extension_boundary():  # +30 min -> advise_not_needed; +31 min -> ALLOW; +30 min with customer_insists=True -> ALLOW
def test_max_days_boundary():  # +14 days -> ALLOW; +14 days 1 min -> escalate extension_too_long
def test_post_quote_boundaries():  # total 500.00 -> ALLOW; 500.01 -> high_value; extension_days 15 -> extension_too_long; 14 -> ALLOW
def test_availability():  # None -> availability_unconfirmed; available False -> availability_unconfirmed; True -> ALLOW
def test_overdue_checked_first():  # overdue now AND new <= CUR -> overdue_rental
```

  Each test asserts exactly what its comment states.
- [ ] **Step 2: Run to verify they fail**
  Run: `python -m pytest tests/test_guardrails.py -v`
  Expected: FAIL with `ModuleNotFoundError: No module named 'guardrails'`
- [ ] **Step 3: Implement `src/guardrails.py`** per the Interfaces block (pure functions, no I/O).
- [ ] **Step 4: Run to verify they pass**
  Run: `python -m pytest tests/test_guardrails.py -v`
  Expected: 7 passed
- [ ] **Step 5: Commit**
  ```bash
  git add src/guardrails.py tests/test_guardrails.py
  git commit -m "feat: add extension guardrails"
  ```

### Task 6: Conversation state and handoffs

**Files:**
- Create: `src/session.py`
- Create: `src/handoff.py`
- Test: `tests/test_session_handoff.py`

**Depends on:** Task 3

**Interfaces:**
- Consumes: `SessionLogger`, `mask_email`, `redact` (Task 3).
- Produces in `src/session.py`:
  - `@dataclass class Quote: token: str; new_return_utc: datetime; new_return_local: str; charges: dict; created_at: datetime; status: str = "presented"; attempts: int = 0`. Statuses: `presented`, `superseded`, `declined`, `committed`, `unknown`, `voided`.
  - `@dataclass class ConversationState: session_id: str; logger: SessionLogger | None = None; verification: str = "unverified"` (`unverified` | `verified` | `email_unconfirmed`), `reservation_id: str | None = None`, `email: str | None = None`, `reservation: dict | None = None`, `zone: tzinfo | None = None`, `email_failures: int = 0`, `payment_failures: int = 0`, `verify_attempts: int = 0`, `quotes: list[Quote] = field(default_factory=list)`, `handoffs: dict[str, str] = field(default_factory=dict)` (reason → reference), `handoff_count: int = 0`, `trace: list[dict] = field(default_factory=list)`.
  - `new_session_id() -> str` — `uuid4().hex[:8]`.
  - `ConversationState.record(event: str, level: str = "info", **fields) -> None` — writes through `logger` when set (which redacts) and appends the redacted record to `trace` (use `redact` directly when no logger, adding `event` and `level`).
  - `ConversationState.add_quote(quote: Quote) -> None` — marks every earlier `presented` quote `superseded`, appends; token assigned by caller as `f"q{len(quotes)+1}"`.
  - `ConversationState.latest_quote() -> Quote | None`.
  - `ConversationState.is_verified() -> bool` — `verification in ("verified", "email_unconfirmed")`.
- Produces in `src/handoff.py`:
  - `class HandoffWriter(log_dir: Path)`; `open(state: ConversationState, reason: str, summary: str, *, idempotency_key: str | None = None, charge_status: str | None = None, now: datetime | None = None) -> dict` returning `{"handoff_reference": str, "path": str, "already_open": bool}`. If `reason in state.handoffs`, return the existing reference with `already_open=True` and write nothing. Otherwise increment `handoff_count` (n), write `<log_dir>/handoffs/<session_id>-<n>.json` with keys `reference` (`HO-<session_id>-<n>`), `timestamp`, `reason`, `summary`, `reservation_id`, `verified` (bool), `email_masked`, `latest_quote` (token, new_return_local, charges, status, or null), `idempotency_key`, `charge_status`, `recent_events` (last 10 of `state.trace`); pass the whole record through `redact` before writing; store the reference in `state.handoffs`; `state.record("handoff", reason=..., reference=...)`.

- [ ] **Step 1: Write the failing tests** in `tests/test_session_handoff.py`

```python
def test_new_quote_supersedes_old():  # add q1, add q2 -> q1.status == "superseded", latest_quote() is q2
def test_record_appends_redacted_trace(tmp_path):  # record("tool_call", args={"email": "a.b@example.com"}) -> trace[-1]["args"]["email"] == "a***@example.com"; log file has one line
def test_handoff_written(tmp_path):  # open(state, "overdue_rental", "summary") -> file logs/handoffs/<sid>-1.json exists; reference "HO-<sid>-1"; email_masked; recent_events len <= 10
def test_handoff_dedup_per_reason(tmp_path):  # second open with same reason -> already_open True, same reference, still 1 file; different reason -> "-2" file
def test_handoff_never_contains_card_fields(tmp_path):  # trace containing {"payment": {"cvv": "847"}} -> "847" not in file text
```

  Each test asserts exactly what its comment states.
- [ ] **Step 2: Run to verify they fail**
  Run: `python -m pytest tests/test_session_handoff.py -v`
  Expected: FAIL with `ModuleNotFoundError: No module named 'session'`
- [ ] **Step 3: Implement `src/session.py` and `src/handoff.py`** per the Interfaces block.
- [ ] **Step 4: Run to verify they pass**
  Run: `python -m pytest tests/test_session_handoff.py -v`
  Expected: 5 passed
- [ ] **Step 5: Commit**
  ```bash
  git add src/session.py src/handoff.py tests/test_session_handoff.py
  git commit -m "feat: add conversation state and handoff records"
  ```

### Task 7: Payment collection outside the model

**Files:**
- Create: `src/payment.py`
- Test: `tests/test_payment.py`

**Depends on:** none

**Interfaces:**
- Produces in `src/payment.py`:
  - `@dataclass(frozen=True) class PaymentDetails: cvv: str = field(repr=False); billing_zip: str = field(repr=False)`.
  - `class PaymentCollector(Protocol): def collect(self, amount: float, currency: str, card_type: str, last_four: str, description: str) -> PaymentDetails | None`.
  - `class CliPaymentCollector(read_secret: Callable[[str], str] | None = None, print_fn: Callable[[str], None] = print)`: prints `description` and `Authorize a charge of <currency> <amount:.2f> to your <card_type> ending <last_four>. Press Enter with no input to cancel.`; reads CVV then ZIP with `read_secret`. Default `read_secret` is `getpass.getpass` when `sys.stdin.isatty()`, else `input` after printing `(input is not masked: stdin is not a terminal)` once — this lets the Task 13 transcripts be captured with piped input. Empty CVV or empty ZIP → `None` (decline). Format check CVV `^\d{3,4}$`, ZIP `^\d{5}$`; one re-prompt of the invalid field; still invalid → `None`.
  - `class ScriptedPaymentCollector(responses: list[PaymentDetails | None])` — returns the next item per call (`None` once exhausted) and records each call's `(amount, card_type, last_four)` in `.calls`.

- [ ] **Step 1: Write the failing tests** in `tests/test_payment.py`

```python
def test_cli_collects_and_restates_amount():  # read_secret returns "847","90210" -> PaymentDetails; printed text contains "USD 100.49" and "Visa ending 4832"
def test_cli_empty_is_decline():  # read_secret returns "" -> None
def test_cli_reprompts_once():  # "12", "847", "90210" -> PaymentDetails(cvv="847"); "12","1","90210" -> None
def test_repr_hides_values():  # "847" not in repr(PaymentDetails("847", "90210"))
def test_scripted():  # ScriptedPaymentCollector([PaymentDetails("847","90210"), None]) returns them in order, then None
```

  Each test asserts exactly what its comment states.
- [ ] **Step 2: Run to verify they fail**
  Run: `python -m pytest tests/test_payment.py -v`
  Expected: FAIL with `ModuleNotFoundError: No module named 'payment'`
- [ ] **Step 3: Implement `src/payment.py`** per the Interfaces block.
- [ ] **Step 4: Run to verify they pass**
  Run: `python -m pytest tests/test_payment.py -v`
  Expected: 5 passed
- [ ] **Step 5: Commit**
  ```bash
  git add src/payment.py tests/test_payment.py
  git commit -m "feat: add masked payment collection outside the model"
  ```

### Task 8: Knowledge-base retrieval

**Files:**
- Create: `src/kb.py`
- Test: `tests/test_kb.py`

**Depends on:** none

**Interfaces:**
- Produces in `src/kb.py`:
  - `AUTHORITY_BONUS = {"official-policy": 0.05, "help-center": 0.0, "legacy": -0.05}`.
  - `Embedder = Callable[[list[str]], list[list[float]]]`; `openai_embedder(model: str) -> Embedder` (wraps `openai.OpenAI().embeddings.create(model=..., input=texts)`; client created lazily on first call).
  - `class KnowledgeBase(articles: list[dict], embed: Embedder | None, cache_path: Path | None, on_event: Callable[..., None] | None = None)`; `KnowledgeBase.load(kb_path: Path, embed, cache_path, on_event=None)`.
  - `search(query: str, k: int = 3) -> dict` returning `{"outcome": "results", "fallback": bool, "articles": [{"id","title","authority","last_updated","body","score"}]}`.
  - Embedding path: article text `title + "\n" + body`; cache JSON `{sha256(text): vector}` at `cache_path` (read once, missing entries embedded in one batch call and written back); score = cosine(query, article) + bonus.
  - Fallback (embed is `None` or raises): tokenise lower-case `[a-z0-9]+`, drop stopwords `a an the and or of for to in on at by is are be it if your you my i we our with from as that this can do does how what when`, strip one trailing `s` from words longer than 3 letters; score = |query ∩ article tokens| / |query tokens| + bonus, matching only when the overlap is > 0; then drop each `legacy` match whose `category` also has a matching `official-policy` article.
  - Ordering in both paths: score descending, then `last_updated` descending, then `id` ascending. Scores rounded to 4 places in output.
  - Every search calls `on_event("kb_search", query=..., fallback=..., results=[{"id","score","authority"}...])`; a fallback also calls `on_event("error", level="warning", message="embeddings unavailable; keyword fallback", error=str(exc))` when an exception caused it.
  - No extra legacy penalty in the embeddings path: the spec fixes the bonus at −0.05, and the live `grace_question` eval (Task 12) checks the real ranking. If that eval fails on ranking, stop and report it rather than changing the bonus.

- [ ] **Step 1: Write the failing tests** in `tests/test_kb.py` (real `data/knowledge-base/articles.json` for fallback tests; stub embedders, no network)

```python
def test_authority_bonus_orders_equal_similarity():
    arts = [art("a", "legacy"), art("b", "help-center"), art("c", "official-policy")]
    kb = KnowledgeBase(arts, embed=lambda texts: [[1.0, 0.0]] * len(texts), cache_path=None)
    assert [a["id"] for a in kb.search("x")["articles"]] == ["c", "b", "a"]

def test_keyword_fallback_grace_question():
    kb = KnowledgeBase.load(KB_PATH, embed=None, cache_path=None)
    out = kb.search("grace period for late returns")
    ids = [a["id"] for a in out["articles"]]
    assert out["fallback"] and ids and "kb_ext_01" in ids and "kb_fee_01" not in ids

def test_fallback_on_embedder_error_logs():  # embed raises RuntimeError -> fallback True; on_event got an "error" event and a "kb_search" event
def test_cache_reused(tmp_path):  # two KnowledgeBase instances on same cache_path: embedder called for articles only the first time (count calls with len(texts) > 1)
def test_results_carry_metadata():  # each article dict has id, title, authority, last_updated, body
```

  Each test asserts exactly what its comment states (`art(id, authority)` is a local helper building a minimal article dict with a shared `category`).
- [ ] **Step 2: Run to verify they fail**
  Run: `python -m pytest tests/test_kb.py -v`
  Expected: FAIL with `ModuleNotFoundError: No module named 'kb'`
- [ ] **Step 3: Implement `src/kb.py`** per the Interfaces block (pure Python cosine; no numpy).
- [ ] **Step 4: Run to verify they pass**
  Run: `python -m pytest tests/test_kb.py -v`
  Expected: 5 passed
- [ ] **Step 5: Commit**
  ```bash
  git add src/kb.py tests/test_kb.py
  git commit -m "feat: add authority-aware KB retrieval with keyword fallback"
  ```

### Task 9: Tools part 1 — verify_customer and quote_extension

**Files:**
- Create: `src/tools.py`
- Modify: `tests/fakes.py`
- Test: `tests/test_tools_verify_quote.py`

**Depends on:** Task 1, Task 2, Task 3, Task 4, Task 5, Task 6, Task 7, Task 8

**Interfaces:**
- Consumes: everything from Tasks 1–8 by the names in their Interfaces blocks.
- Produces in `src/tools.py`:
  - `@dataclass class ToolDeps: state: ConversationState; client: RoadwayClient; payments: PaymentCollector; kb: KnowledgeBase; handoffs: HandoffWriter; settings: Settings; now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)`.
  - `verify_customer_impl(deps: ToolDeps, reservation_id: str, email: str) -> dict`
  - `quote_extension_impl(deps: ToolDeps, new_return_local: str, customer_insists: bool = False) -> dict`
  - Each `*_impl` records `tool_call` (`tool=<name>`, `args`) first and `tool_result` (`tool=<name>`, `result`) last. Every guardrail evaluation records `record("guardrail", check=<"pre_quote" | "availability" | "post_quote">, verdict=<Verdict.kind>, reason=<Verdict.reason>)` — plain strings, never the `Verdict` object.
- Produces in `tests/fakes.py`: `FakeRoadwayServer(reservations: dict[str, dict], emails: dict[str, str], availability: dict | Exception | None = None, quote_charges: dict | None = None)` implementing the same `request(...)` signature as `ScriptedHTTP`, routing GET `/reservations/{id}` (404 envelope when unknown), GET `/availability`, POST `/quote`, POST `/extend` (403 on wrong email; 400 `PAYMENT_VALIDATION_ERROR` when `payment` is missing; 409 `RESERVATION_NOT_ACTIVE` when status is not `active`; otherwise 200 with `confirmation_number`, `extension_details`, `charges`). It records every call in `.calls` and every successful extend in `.commits` (list of `(reservation_id, idempotency_key)`), replaying a stored response when an `Idempotency-Key` repeats. Two further keyword arguments let tests force responses: `overrides: dict[str, list[FakeResponse | Exception]] | None` keyed by endpoint `"reservation"` (GET reservation), `"availability"`, `"quote"`, `"extend_probe"` (extend without `payment`) or `"extend_payment"` (extend with `payment`) — each call to that endpoint first pops the next queued item (raising it if it is an exception; an override 2xx on `extend_payment` also counts as a commit) and falls back to normal routing when the queue is empty; and `extend_charges: dict | None` replacing the `charges` block of a normal successful extend (default: the same charges as the quote, total 100.49). Default quote charges are the `docs/api-reference.md` example (`extension_days` 2, `total_charged` 100.49, `late_fee` 0); `quote_charges` and `extend_charges` are merged over those defaults. Plus `make_reservation(**overrides) -> dict` returning the `RWR-29471835` example from `docs/api-reference.md` and `make_deps(server, tmp_path, now, collector=None) -> ToolDeps` (state with a `SessionLogger` under `tmp_path`; real `RoadwayClient` over the fake with `sleep=lambda s: None` and `on_attempt` wired to `state.record("api_attempt", ...)`; KB with `embed=None` and `on_event=state.record`; `HandoffWriter(tmp_path)`; collector defaults to `ScriptedPaymentCollector([PaymentDetails("847", "90210")] * 3)`).

**verify_customer_impl behaviour** (spec "Tools"; answers plan questions on GET 5xx, eligibility and outcomes):
1. Normalise ID (`strip().upper()`) and email (`strip().lower()`); `verify_attempts += 1`; store the normalised ID in `state.reservation_id` (discloses nothing; lets handoffs carry it).
2. `GET /reservations/{id}`: 404 → wrong-email handling (step 5); any other `RoadwayAPIError` → `system_unavailable` (no failure count, no handoff).
3. Probe: `extend(id, email=email, new_return_datetime=to_api(max(current_return, now) + 1 day, zone), payment=None, idempotency_key=f"verify-{session_id}-{verify_attempts}")`.
4. Map, evaluating in the order listed (so a non-active booking gives `not_eligible` even when the probe returns 400 `PAYMENT_VALIDATION_ERROR`): 403 → step 5. 409 `RESERVATION_NOT_ACTIVE`, or GET status ≠ `active` and the probe did not return 403 → `not_eligible`. 400 `PAYMENT_VALIDATION_ERROR` → verified. Any other 4xx on an `active` booking → verified with `verification = "email_unconfirmed"` and `email_confirmed: false`. 5xx/transport after retries → `system_unavailable`. 2xx → `record("error", level="error", ...)`, open handoff `unexpected_charge_on_verification`, return `escalated`.
5. Wrong email: `email_failures += 1`; at `max_email_failures` open handoff `verification_failed` → `escalated`; else `not_verified` with `attempts_remaining`.
6. On verified: set `reservation_id`, `email`, `reservation`, `zone = zone_for(return code, pickup_datetime)`, mark every existing quote `voided`; return `outcome: verified`, `email_confirmed`, `customer_name`, `membership_tier`, `vehicle` (description), `vehicle_type`, `return_location` (`code`, `name`, `time_zone` = `zone_label`), `current_return_local`, `daily_rate`, `currency`, `card_type`, `card_last_four`, `now_local`. No booking detail appears in any non-`verified` result.
7. The overdue check is **not** run here; it stays a pre-quote guardrail as the spec's table places it. The `overdue` eval script supplies a date (Task 12).

**quote_extension_impl behaviour** (answers plan questions on availability dates and quote errors):
1. Not `is_verified()` → `verification_required`.
2. `parse_local(new_return_local, zone)`; `InvalidLocalTime` → `invalid_datetime` with `hint`.
3. `pre_quote(current_return, new_return, now, settings, customer_insists)`: `invalid` → `invalid_request` (`reason`, plus `message` "The new return time must be later than the current return time, <current_return_local>."); `advise_not_needed` → return immediately (no availability or quote call) with `message` "Returns within 30 minutes of the scheduled time are covered by the grace period, and an extension bills a full day. If the customer still wants it, call again with customer_insists=true."; `escalate` → handoff with that reason → `escalated`.
4. Availability: `start_date` = local date (return zone) of the current return; `end_date` = local date of the new return. Any `RoadwayAPIError` → unknown. `availability_verdict`; escalate → handoff `availability_unconfirmed`, result includes `alternatives` (`alternative_types_at_location` and nearby location codes when present).
5. `quote_extension(id, to_api(new_return, zone))`: 4xx → `quote_failed` (`code`, `message`; no handoff); 5xx/transport → `system_unavailable` (no handoff).
6. `post_quote(charges)`: escalate → handoff → `escalated`.
7. `add_quote(Quote(token=f"q{n}", ...))` → `quoted` with `quote_token`, `new_return_local`, `time_zone`, `extension_days`, `daily_rate`, `subtotal`, `late_fee`, `taxes_and_fees`, `total_charged`, `currency`, `card_type`, `card_last_four`, `expires_in_minutes`.

- [ ] **Step 1: Write the failing tests** in `tests/test_tools_verify_quote.py`, with `NOW = 2027-06-14T12:00-07:00` and the default reservation (LAX, return `2027-06-15T14:00-07:00`, email `sarah.johnson@example.com`):

```python
def test_verify_success_returns_summary():  # outcome verified, email_confirmed True, card_last_four "4832", current_return_local "2027-06-15T14:00", time_zone "America/Los_Angeles"; probe call has no "payment" and key "verify-<sid>-1"
def test_verify_normalises_inputs():  # " rwr-29471835 ", "Sarah.Johnson@Example.com " -> verified
def test_verify_wrong_email_then_handoff():  # first -> not_verified attempts_remaining 1, no booking keys in result; second -> escalated reason verification_failed, handoff file exists
def test_verify_404_same_as_wrong_email():  # unknown ID -> not_verified, email_failures == 1
def test_verify_not_active():  # status "completed" -> not_eligible, result has no customer_name
def test_verify_other_4xx_falls_back():  # overrides {"extend_probe": [400 INVALID_EXTENSION]} -> verified, email_confirmed False, state.verification == "email_unconfirmed"
def test_verify_get_5xx_system_unavailable():  # overrides {"reservation": [503 x3]} -> system_unavailable, email_failures 0, no handoff
def test_verify_probe_2xx_escalates():  # overrides {"extend_probe": [FakeResponse(200, {...success body...})]} -> escalated unexpected_charge_on_verification
def test_verify_unmapped_location_uses_pickup_offset():  # return code "ZZZ", pickup "...-05:00", current_return "2027-06-15T19:00:00Z" -> current_return_local "2027-06-15T14:00", time_zone "UTC-05:00"
def test_quote_requires_verification():  # -> verification_required, no /quote call
def test_quote_rejects_bad_format():  # "June 17 at 2pm" and "2027-06-17 14:00" -> invalid_datetime with "YYYY-MM-DDTHH:MM" in hint; no /availability or /quote call
def test_quote_happy():  # "2027-06-17T14:00" -> quoted, total_charged 100.49, quote_token "q1"; /quote body new_return_datetime "2027-06-17T14:00:00-07:00"; /availability params start_date "2027-06-15", end_date "2027-06-17"; trace contains an event with event "guardrail", check "availability", verdict "allow"
def test_quote_not_later_invalid_request():  # "2027-06-15T13:00" -> invalid_request, no handoff, no /quote call
def test_quote_short_extension_advised():  # "2027-06-15T14:20" -> advise_not_needed, no /availability call; with customer_insists=True -> quoted
def test_quote_overdue_escalates():  # NOW = current return + 31 min -> escalated overdue_rental, no /availability call
def test_quote_too_long_escalates():  # +15 days -> escalated extension_too_long
def test_quote_high_value_escalates():  # quote_charges total 500.01 -> escalated high_value, no quote stored as presented
def test_quote_availability_unknown_escalates():  # availability=requests.Timeout() -> escalated availability_unconfirmed
def test_quote_unavailable_escalates_with_alternatives():  # available False -> escalated, alternatives non-empty
def test_quote_4xx_and_5xx():  # overrides {"quote": [400 INVALID_DATE]} -> quote_failed; overrides {"quote": [503 x3]} -> system_unavailable; neither opens a handoff
def test_requote_supersedes():  # two quotes -> q1 superseded, q2 presented
```

  Each test asserts exactly what its comment states.
- [ ] **Step 2: Run to verify they fail**
  Run: `python -m pytest tests/test_tools_verify_quote.py -v`
  Expected: FAIL with `ModuleNotFoundError: No module named 'tools'`
- [ ] **Step 3: Implement `FakeRoadwayServer`, `make_reservation`, `make_deps` in `tests/fakes.py` and `ToolDeps`, `verify_customer_impl`, `quote_extension_impl` in `src/tools.py`** per the behaviour lists above.
- [ ] **Step 4: Run to verify they pass**
  Run: `python -m pytest tests/test_tools_verify_quote.py -v`
  Expected: 21 passed
- [ ] **Step 5: Commit**
  ```bash
  git add src/tools.py tests/fakes.py tests/test_tools_verify_quote.py
  git commit -m "feat: add verify_customer and quote_extension tools"
  ```

### Task 10: Tools part 2 — confirm_extension, KB and escalation tools, fault injection

**Files:**
- Modify: `src/tools.py`
- Modify: `tests/fakes.py`
- Test: `tests/test_tools_confirm.py`
- Test: `tests/test_fault_injection.py`

**Depends on:** Task 9

**Interfaces:**
- Consumes: `ToolDeps`, `FakeRoadwayServer`, `make_deps` (Task 9); `ScriptedPaymentCollector` (Task 7).
- Produces in `src/tools.py`:
  - `confirm_extension_impl(deps: ToolDeps, quote_token: str) -> dict`
  - `search_knowledge_base_impl(deps: ToolDeps, query: str) -> dict` — `deps.kb.search(query, k=3)`; `on_event` of the KB is wired to `state.record` by the caller that builds `ToolDeps` (Task 11).
  - `escalate_to_human_impl(deps: ToolDeps, reason: str, summary: str) -> dict` — `handoffs.open(...)` → `{"outcome": "escalated", "handoff_reference", "already_open"}`. Works without verification; carries `reservation_id` only if one was given to `verify_customer` (spec gate question: out-of-scope requests are handed off **without** verifying first).
- Produces in `tests/fakes.py`: `FakeRoadwayServer(..., extend_faults: Callable[[], str | None] | None = None)`. Before handling a payment-bearing extend, it calls `extend_faults()`; `"5xx_before"` → 503 without committing; `"timeout_before"` → raise `requests.Timeout` without committing; `"timeout_after"` → commit (stored under the key) then raise `requests.Timeout` (response lost); `None` → normal. A retry with a committed key replays the stored response without a new commit. This answers the spec gate question: yes, server-side commit before a client-side failure is simulated, with key replay.

**confirm_extension_impl behaviour:**
1. Not `is_verified()` → `verification_required`.
2. Token must equal `latest_quote().token` (else `no_valid_quote` reason `superseded`, or `no_quote` when there is none), status `presented` (else `not_presented`), and `now - created_at < quote_ttl_minutes` (else `expired`). No API call on refusal.
3. `quote.attempts += 1`; `details = payments.collect(total_charged, currency, card_type, last_four, description=f"Extend {reservation_id} to {new_return_local}")`. `None` → status `declined`, `customer_declined`.
4. Key `f"ext-{session_id}-{token}-{attempts}"`; `extend(..., payment={"use_card_on_file": True, "cvv": details.cvv, "billing_zip": details.billing_zip}, idempotency_key=key)`. CVV/ZIP stay in local variables; `tool_call` args record only `quote_token`.
5. 2xx → status `committed`; `committed` with `confirmation_number`, `new_return_local`, `charges`, `late_return`, `amount_differs_from_quote` (abs difference of `total_charged` > 0.01; when true also `record("tool_result"... level="warning")`).
6. 402 `PAYMENT_DECLINED` / 400 `PAYMENT_VALIDATION_ERROR` → `payment_failures += 1`; at `max_payment_failures` handoff `payment_failed` → `escalated`; else `payment_failed` with `attempts_remaining` (quote stays `presented`; the next call uses attempt+1 and a new key).
7. 403 → verify-at-commit failure (answers the spec gate question): quote status `voided`; `verification = "unverified"`; `email_failures += 1`; at the limit handoff `verification_failed` → `escalated`; else `not_verified` with `message` "The email did not match. Verify again with verify_customer, then get a new quote." The customer goes back through `verify_customer` then `quote_extension`.
8. 409 `RESERVATION_NOT_ACTIVE`, 400 `INVALID_EXTENSION`, 400 `INVALID_DATE`, any other 4xx → `rejected` with `code` and `message` (no handoff; the agent offers one).
9. 5xx or transport after retries → status `unknown`; handoff `charge_status_unknown` with `idempotency_key=key`, `charge_status="unknown"`; `escalated` with `message` "We could not confirm whether the extension went through. It will not be retried; a representative will check and follow up."

- [ ] **Step 1: Write the failing tests** in `tests/test_tools_confirm.py` (state verified and quoted via the Task 9 impls; NOW fixed)

```python
def test_confirm_happy():  # collector PaymentDetails("9731", "60614"); committed, confirmation_number present, 1 commit, key "ext-<sid>-q1-1"; neither "9731" nor "60614" appears as a value in result, and no parsed session-log record or handoff contains a key cvv/billing_zip/payment or a string value equal to "9731"/"60614"
def test_confirm_refuses_without_token():  # "" and "q9" -> no_valid_quote, collector not called, no extend call
def test_confirm_refuses_superseded():  # q1 after q2 exists -> no_valid_quote reason superseded
def test_confirm_refuses_stale():  # now = created + 15 min -> expired; created + 14:59 -> committed
def test_confirm_after_decline():  # collector returns None -> customer_declined; second confirm -> no_valid_quote not_presented; 0 commits
def test_confirm_twice_charges_once():  # committed, then same token -> no_valid_quote; exactly 1 extend call with payment
def test_payment_failed_then_retry_new_key():  # overrides {"extend_payment": [402 PAYMENT_DECLINED]} then normal routing -> payment_failed attempts_remaining 1, then committed; keys "-1" and "-2"
def test_payment_failed_twice_escalates():  # overrides {"extend_payment": [402, 402]} -> escalated payment_failed
def test_verify_at_commit_403():  # verify with overrides {"extend_probe": [400 INVALID_EXTENSION]} and a wrong email (state becomes email_unconfirmed), quote, then confirm: the fake returns 403 on the payment extend -> not_verified, quote voided, verification "unverified", email_failures 1
def test_rejected_codes():  # one fresh quoted state per code, overrides {"extend_payment": [409 RESERVATION_NOT_ACTIVE]} / [400 INVALID_EXTENSION] / [400 INVALID_DATE] -> rejected with that code
def test_unknown_outcome_hands_off_no_second_key():  # overrides {"extend_payment": [503 x3]} -> escalated charge_status_unknown; handoff file has the key and charge_status "unknown"; all 3 calls same key; confirm again -> no_valid_quote, no new call
def test_amount_differs_flagged():  # extend_charges total_charged 101.00 vs quote 100.49 -> committed, amount_differs_from_quote True, warning event in trace
def test_escalate_dedup():  # escalate_to_human("cancel_request", ...) twice -> second already_open True
def test_escalate_without_verification():  # fresh state -> escalated, handoff verified False
def test_search_kb_tool():  # returns outcome "results", <= 3 articles, records kb_search
```

  Each test asserts exactly what its comment states.
- [ ] **Step 2: Write the failing fault-injection test** in `tests/test_fault_injection.py`

```python
def test_fault_injection_200_confirms(tmp_path):
    rng = random.Random(1234)
    faults = lambda: (rng.choice(["5xx_before", "timeout_before", "timeout_after"]) if rng.random() < 0.30 else None)
    # 200 iterations: fresh FakeRoadwayServer(extend_faults=faults) and fresh verified+quoted state per call
    # (verify/quote run with faults disabled), ScriptedPaymentCollector([PaymentDetails("847", "90210")]),
    # then confirm_extension_impl(deps, "q1").
    # Assert per call: len(server.commits) <= 1; the payment-bearing extend calls all carry one identical key;
    # result outcome in {"committed", "escalated"}; when escalated, reason == "charge_status_unknown" and a handoff file exists.
    # Assert overall: at least one committed and every outcome counted; print the tally.
```

- [ ] **Step 3: Run to verify they fail**
  Run: `python -m pytest tests/test_tools_confirm.py tests/test_fault_injection.py -v`
  Expected: FAIL with `ImportError: cannot import name 'confirm_extension_impl'`
- [ ] **Step 4: Implement** the fault hook in `tests/fakes.py` and `confirm_extension_impl`, `search_knowledge_base_impl`, `escalate_to_human_impl` in `src/tools.py` per the behaviour list.
- [ ] **Step 5: Run to verify they pass**
  Run: `python -m pytest tests/test_tools_confirm.py tests/test_fault_injection.py -v`
  Expected: 16 passed
- [ ] **Step 6: Commit**
  ```bash
  git add src/tools.py tests/fakes.py tests/test_tools_confirm.py tests/test_fault_injection.py
  git commit -m "feat: add confirm_extension, KB and escalation tools with fault injection"
  ```

### Task 11: Agent definition and CLI chat loop

**Files:**
- Modify: `src/agent.py`
- Test: `tests/test_agent.py`

**Depends on:** Task 9, Task 10

**Interfaces:**
- Consumes: the five `*_impl` functions and `ToolDeps` (Tasks 9–10); `Settings`, `load_settings` (Task 1); `RoadwayClient` (Task 4); `ConversationState`, `new_session_id` (Task 6); `HandoffWriter` (Task 6); `SessionLogger` (Task 3); `KnowledgeBase`, `openai_embedder` (Task 8); `CliPaymentCollector` (Task 7).
- Produces in `src/agent.py`:
  - Five `@function_tool` wrappers named exactly `verify_customer`, `quote_extension(new_return_local: str, customer_insists: bool = False)`, `confirm_extension`, `search_knowledge_base`, `escalate_to_human`, each `(ctx: RunContextWrapper[ToolDeps], ...)` calling the matching `*_impl(ctx.context, ...)` and wrapping any unexpected exception as `record("error", level="error")` plus `{"outcome": "system_unavailable"}`. Docstrings tell the model what each tool does and its outcome strings.
  - `INSTRUCTIONS: str` covering: persona (concise, warm, plain numbers); scope (extensions only; cancel, modify, upgrade, class change, other location, other card, complaints and "talk to a person" → answer any policy question with `search_knowledge_base`, then `escalate_to_human` **without** verifying first); collect reservation ID and email before saying anything about a booking; ask for the new return as a local date and time and pass it as `YYYY-MM-DDTHH:MM`, resolving relative dates from `now_local`; read the exact quote (days, subtotal, late fee, taxes, total, card type and last four) and require an explicit yes before `confirm_extension`; tell the customer a secure prompt will ask for CVV and ZIP and never to type card details in chat; never promise anything a tool did not return; for `advise_not_needed` explain the grace period and only re-call with `customer_insists=true` if the customer insists; handling for every outcome in the "Tool outcome vocabulary" table; always read the `handoff_reference` to the customer on `escalated`; KB precedence (official-policy over help-center over legacy, cite article IDs, never quote a legacy figure an official article contradicts, e.g. the grace period is 30 minutes per `kb_ext_01`, not 2 hours).
  - `build_agent(settings: Settings) -> Agent[ToolDeps]` (name "Roadway Assistant", model `settings.openai_model`).
  - `build_deps(settings: Settings, payments: PaymentCollector, session_id: str | None = None) -> ToolDeps` — creates state with `SessionLogger`, client with `on_attempt=lambda a: state.record("api_attempt", **a)`, KB with `openai_embedder(settings.embedding_model)`, cache `<cache_dir>/kb_embeddings.json`, `on_event=state.record`, `HandoffWriter(log_dir)`.
  - `run_turn(agent, deps: ToolDeps, history: list, user_text: str, runner=Runner) -> tuple[str, list]` — records `turn_user`, calls `runner.run_sync(agent, history + [{"role": "user", "content": user_text}], context=deps, max_turns=20)`, records `turn_agent`, returns `(result.final_output, result.to_input_list())`.
  - `chat_loop(agent, deps, input_fn=input, print_fn=print, runner=Runner) -> None` — prints `Session <id>; log: <path>`; loops until `exit`/`quit` (case-insensitive) or `EOFError`; skips blank lines.
  - `main()` — `load_dotenv()`, require `OPENAI_API_KEY` and `ROADWAY_API_KEY` (print a hint and return if missing), build and run.

- [ ] **Step 1: Write the failing tests** in `tests/test_agent.py` (no network: fake runner object with `run_sync` returning an object with `final_output` and `to_input_list()`)

```python
def test_agent_has_exactly_five_tools():  # {t.name for t in build_agent(load_settings({})).tools} == {"verify_customer","quote_extension","confirm_extension","search_knowledge_base","escalate_to_human"}
def test_instructions_cover_rules():  # INSTRUCTIONS contains "kb_ext_01", "30 minutes", "explicit yes", "card details", "escalate_to_human", "customer_insists"
def test_chat_loop_logs_turns_and_exits(tmp_path):  # inputs ["hi", "", "quit"]; fake runner called once; trace has turn_user and turn_agent; first printed line starts with "Session "
def test_chat_loop_eof():  # input_fn raises EOFError -> returns cleanly
def test_history_carried():  # second run_turn passes the first call's to_input_list() plus the new user message
```

  Each test asserts exactly what its comment states; `deps` built by `make_deps` from `tests/fakes.py`.
- [ ] **Step 2: Run to verify they fail**
  Run: `python -m pytest tests/test_agent.py -v`
  Expected: FAIL with `ImportError: cannot import name 'build_agent'`
- [ ] **Step 3: Rewrite `src/agent.py`** per the Interfaces block.
- [ ] **Step 4: Run to verify they pass, then the full suite**
  Run: `python -m pytest -q`
  Expected: all tests pass (`0 failed`)
- [ ] **Step 5: Commit**
  ```bash
  git add src/agent.py tests/test_agent.py
  git commit -m "feat: agent definition, instructions and CLI chat loop"
  ```

### Task 12: Scenario evals

**Files:**
- Create: `evals/__init__.py`
- Create: `evals/scenarios.py`
- Create: `evals/run_evals.py`
- Test: `tests/test_eval_checks.py`

**Depends on:** Task 11

**Interfaces:**
- Consumes: `build_agent`, `build_deps`, `run_turn` (Task 11); `ScriptedPaymentCollector`, `PaymentDetails` (Task 7); `timeutil` functions (Task 2); `RoadwayClient` (Task 4).
- Produces:
  - `evals/__init__.py`: inserts `<repo>/src` at `sys.path[0]` if absent (answers the import question).
  - `evals/scenarios.py`: `@dataclass class Scenario: name: str; reservation_id: str | None; email: str | None; messages: list[str]` (templates with `{rid}`, `{email}`, `{new_local}`); `extend_by: timedelta | None`; `payments: list[PaymentDetails | None]`; `check: Callable[[list[dict], str], tuple[str, str]]` returning `(status, detail)` with status `pass`, `fail`, or `skipped (precondition)`; the second argument is the concatenated agent replies. `SCENARIOS: list[Scenario]` with the ten names from the spec. Helper `new_return_local(reservation: dict, now: datetime, delta: timedelta) -> str` = `format_local(max(current_return, now) + delta, zone)` rounded down to the minute.
  - Trace helpers in `evals/scenarios.py`: `tool_results(trace, name) -> list[dict]`, `commits(trace) -> int` (count of `confirm_extension` results with outcome `committed`), `handoff_reasons(trace) -> list[str]` (from `handoff` events), `kb_result_ids(trace) -> list[list[str]]`.
  - `evals/run_evals.py`: `run_scenario(settings: Settings, scenario: Scenario, run_index: int) -> dict` (one row: `scenario`, `run`, `status`, `detail`, `session_id`) and `main(argv=None) -> int` (calls `run_scenario` through the module attribute so tests can monkeypatch it) with `--scenario NAME` and `--runs N` (default 1). For each scenario and run: fresh `build_deps(settings, ScriptedPaymentCollector(s.payments))`; if `extend_by` is set, `GET` the reservation to compute `{new_local}`; send each message with `run_turn`; apply `check`. Prints a table `scenario | run | status | detail` and writes `logs/evals/<YYYYmmddTHHMMSS>.json` (list of rows plus session IDs). Exit code 0 iff every row is `pass` or `skipped (precondition)`.
- Scenario scripts and checks (payments default `[PaymentDetails("847", "90210")]`):

| Scenario | Messages | Check |
|---|---|---|
| `preferred_happy` | `I'd like to extend my rental.` / `{rid}, {email}` / `Please change the return to {new_local}.` / `Yes, go ahead.` (+2 days) | verify outcome `verified`; a `quoted` result with `total_charged == 100.49` and `late_fee == 0`; `commits == 1` |
| `standard_happy` | same, RWR-66002030, +2 days | `verified`; `commits == 1` |
| `due_soon` | same, RWR-77001020, +2 days | `verified`; `commits == 1` |
| `overdue` | same first three messages, RWR-48372915, +2 days | `"overdue_rental" in handoff_reasons`; `commits == 0` |
| `availability_timeout` | same, RWR-99004050, +2 days | `"availability_unconfirmed" in handoff_reasons` and `commits == 0` → pass; **skipped (precondition)** when the last `guardrail` event with `check == "availability"` has verdict `allow` (the mock answered and the vehicle was available); otherwise fail |
| `declines_quote` | same three, RWR-50000001, +2 days, then `No, I don't want to go ahead.`; payments `[None]` | a `quoted` result; `commits == 0`. The customer declines **in chat**; the `None` payment also declines if the model calls `confirm_extension` anyway |
| `wrong_email_twice` | `I want to extend {rid}. My email is wrong.one@example.com` / `Sorry, try wrong.two@example.com` (RWR-66002030) | `"verification_failed" in handoff_reasons`; `commits == 0` |
| `grace_question` | `How long is the grace period if I return my car late?` | a `kb_search` event exists; the top result of the first `kb_search` has `authority == "official-policy"` (so it ranks above legacy `kb_fee_01`; which official article ranks first does not matter); replies match `30[- ]minute` and do not contain `2 hours`; `commits == 0` |
| `cancel_request` | `I need to cancel reservation {rid}. What is your cancellation policy?` (RWR-29471835) | a `kb_search` event; an `escalate_to_human` tool_result; `commits == 0` |
| `tiny_extension` | first two messages, then `Please change the return to {new_local}.` / `OK, never mind then.` (RWR-66002030, +20 minutes) | a `quote_extension` result with outcome `advise_not_needed`; `commits == 0` |

  Emails come from the `PROBLEM.md` Test accounts table.

- [ ] **Step 1: Write the failing tests** in `tests/test_eval_checks.py` (synthetic traces only)

```python
def test_scenarios_named_as_spec():  # {s.name for s in SCENARIOS} == the ten names
def test_commits_counts_committed_only():  # trace with one committed + one payment_failed -> 1
def test_grace_check_ranking():  # reply "30 minutes": top result legacy kb_fee_01 -> fail; top result help-center -> fail; no kb_search -> fail; top kb_ext_01 -> pass; top kb_fee_02 (official) -> pass; reply containing "2 hours" -> fail
def test_availability_skip_rule():  # guardrail availability verdict allow -> "skipped (precondition)"; handoff availability_unconfirmed -> pass
def test_new_return_local_uses_later_of_now():  # overdue reservation -> based on now, not current return
def test_run_evals_exit_code(monkeypatch):  # monkeypatch evals.run_evals.run_scenario returning pass/skipped rows -> main returns 0; one fail -> 1
```

  Each test asserts exactly what its comment states.
- [ ] **Step 2: Run to verify they fail**
  Run: `python -m pytest tests/test_eval_checks.py -v`
  Expected: FAIL with `ModuleNotFoundError: No module named 'evals'`
- [ ] **Step 3: Implement** `evals/__init__.py`, `evals/scenarios.py`, `evals/run_evals.py` per the Interfaces block and table.
- [ ] **Step 4: Run to verify they pass**
  Run: `python -m pytest tests/test_eval_checks.py -v`
  Expected: 6 passed
- [ ] **Step 5: Run the live evals (success criterion 3)**
  Run: `python -m evals.run_evals --runs 2`
  Expected: exit code 0; every one of the 20 rows is `pass` or (for `availability_timeout` only) `skipped (precondition)`; a file under `logs/evals/`. On a failure, read the session log named in the row, fix prompt or code, re-run the unit suite, then re-run this command; if `grace_question` fails on ranking (legacy or help-center article first), stop and report (Task 8).
- [ ] **Step 6: Commit**
  ```bash
  git add evals tests/test_eval_checks.py
  git commit -m "feat: add scripted scenario evals asserting on the tool trace"
  ```

### Task 13: Manual check, README and final verification

**Files:**
- Modify: `README.md`

**Depends on:** Task 11, Task 12

- [ ] **Step 1: Run three manual conversations (success criterion 4)** with piped input (the payment prompt falls back to unmasked `input` when stdin is not a terminal, Task 7):
  Run: `printf 'I want to extend my rental\nRWR-29471835, sarah.johnson@example.com\nPlease return it two days later at the same time\nyes\n9731\n60614\nquit\n' | python src/agent.py`
  Expected: a session line, a quote with total $100.49, a confirmation number in the reply.
  Run the same for overdue (`RWR-48372915`, `marcus.lee@example.com`, two days later) — Expected: a reply with an `HO-` reference — and for `How long is the grace period if I return late?` — Expected: an answer stating 30 minutes, citing an official-policy article. If a piped conversation desyncs (the model asks an extra question so the CVV is read as a chat turn), delete that session's log file and rerun that conversation interactively in a terminal.
- [ ] **Step 2: Check the logs**
  Run: `grep -rlE '"(cvv|billing_zip|payment)"|"9731"|60614|sarah\.johnson@|marcus\.lee@' logs/sessions logs/handoffs`
  Expected: no output (exit code 1), and `ls logs/sessions logs/handoffs` includes the three session IDs printed in Step 1 (alongside the Task 12 eval sessions) and at least one handoff file from them.
- [ ] **Step 3: Rewrite `README.md`** with sections: Overview; Chosen workflow and why (Extend, from the spec's Purpose); What was cut (spec Scope Out); Design decisions (the spec's Decisions 1–11 and this plan's Tool outcome vocabulary, guardrail table, verify-at-commit fallback, handoff dedup); Setup (Python 3.10+, `pip install -r requirements.txt`, `.env` from `env.example`); How to run (chat `python src/agent.py`, tests `python -m pytest`, evals `python -m evals.run_evals --runs 2`); Configuration (the env vars from Task 1); Logs (`logs/sessions`, `logs/handoffs`, `logs/evals`, `.cache/kb_embeddings.json`, redaction rules); Eval results (the Task 12 table); Sample transcripts (the three from Step 1, pasted); Production next steps (secure payment link or IVR, real handoff queue, persistence).
- [ ] **Step 4: Verify the README against success criterion 5 and run the full suite**
  Run: `python -m pytest -q`
  Expected: `0 failed`. Then confirm by reading that the README covers design decisions, chosen workflow and why, what was cut, how to run (setup, chat, tests, evals) and where logs are.
- [ ] **Step 5: Commit**
  ```bash
  git add README.md
  git commit -m "docs: rewrite README as the project overview"
  ```
