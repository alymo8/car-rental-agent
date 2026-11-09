"""Web demo console. No network: payment collection runs in local threads."""

from __future__ import annotations

import asyncio
import json
import re
import threading
import time
import urllib.error
import urllib.request
from datetime import timedelta

from fakes import FakeRoadwayServer, make_deps, make_reservation
from test_agent import EMAIL, NOW, RID, FakeResult, FakeRunner

import webui
from agent import TURN_ERROR_REPLY, build_agent
from config import load_settings
from payment import PaymentDetails
from tools import confirm_extension_impl, quote_extension_impl, verify_customer_impl
from webui import (
    DEMO_SCENARIOS,
    OVER_LIMIT,
    STATIC_DIR,
    DemoApp,
    DemoSession,
    SharedLoopRunner,
    WebPaymentCollector,
    build_session,
    make_server,
)

ARGS = (142.3, "USD", "Visa", "4421", "Extend RWR-1 to 2027-06-17T14:00")


def _start_collect(collector: WebPaymentCollector) -> tuple[threading.Thread, dict]:
    """Run collect in a thread; wait (<= 2 s) until it is pending."""
    out: dict = {}

    def run() -> None:
        out["result"] = collector.collect(*ARGS)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 2.0
    while collector.pending() is None:
        assert time.monotonic() < deadline, "collect never became pending"
        time.sleep(0.005)
    return thread, out


def _collector_with_items(**kwargs) -> tuple[WebPaymentCollector, list[dict]]:
    items: list[dict] = []
    return WebPaymentCollector(on_request=items.append, **kwargs), items


def test_collect_returns_submitted_details():
    collector, items = _collector_with_items()
    thread, out = _start_collect(collector)
    assert collector.answer("847", "90210") is True
    thread.join(2.0)
    assert out["result"] == PaymentDetails("847", "90210")
    assert items[0]["status"] == "submitted"
    assert collector.pending() is None


def test_collect_cancel_returns_none():
    collector, items = _collector_with_items()
    thread, out = _start_collect(collector)
    assert collector.answer(cancel=True) is True
    thread.join(2.0)
    assert out["result"] is None
    assert items[0]["status"] == "cancelled"


def test_collect_invalid_input_returns_none():
    collector, items = _collector_with_items()
    thread, out = _start_collect(collector)
    assert collector.answer("12", "9021") is True
    thread.join(2.0)
    assert out["result"] is None
    assert items[0]["status"] == "cancelled"


def test_collect_timeout_returns_none():
    collector, items = _collector_with_items(timeout=0.05)
    start = time.monotonic()
    assert collector.collect(*ARGS) is None
    assert time.monotonic() - start < 1.0
    assert collector.pending() is None
    assert items[0]["status"] == "expired"


def test_cancel_pending_returns_none():
    collector, items = _collector_with_items()
    thread, out = _start_collect(collector)
    collector.cancel_pending()
    thread.join(2.0)
    assert out["result"] is None
    assert items[0]["status"] == "cancelled"
    collector.cancel_pending()  # no-op when nothing is pending


def test_answer_without_pending_returns_false():
    collector = WebPaymentCollector()
    assert collector.answer("847", "90210") is False
    thread, out = _start_collect(collector)
    assert collector.answer("847", "90210") is True
    assert collector.answer("111", "11111") is False
    thread.join(2.0)
    assert out["result"] == PaymentDetails("847", "90210")


def test_pending_and_item_never_hold_card_details():
    collector, items = _collector_with_items()
    thread, out = _start_collect(collector)
    pending = collector.pending()
    collector.answer("847", "90210")
    thread.join(2.0)
    assert out["result"] == PaymentDetails("847", "90210")
    for dumped in (json.dumps(items[0]), json.dumps(pending)):
        assert "847" not in dumped
        assert "90210" not in dumped
    assert set(pending) == {
        "amount",
        "currency",
        "card_type",
        "last_four",
        "description",
    }


def test_on_request_receives_public_item():
    collector, items = _collector_with_items()
    thread, _ = _start_collect(collector)
    item = dict(items[0])
    collector.answer(cancel=True)
    thread.join(2.0)
    assert item["role"] == "payment"
    assert item["amount"] == 142.3
    assert item["currency"] == "USD"
    assert item["card_type"] == "Visa"
    assert item["last_four"] == "4421"
    assert item["description"] == ARGS[4]
    assert item["status"] == "pending"


# --- DemoSession / DemoApp -------------------------------------------------


class BlockingRunner:
    def __init__(self):
        self.release = threading.Event()

    def run_sync(self, agent, input, *, context, max_turns):
        self.release.wait(5)
        return FakeResult("late", [*input, {"role": "assistant", "content": "late"}])


class PayingRunner:
    def run_sync(self, agent, input, *, context, max_turns):
        confirm_extension_impl(context, "q1")
        reply = "done"
        return FakeResult(reply, [*input, {"role": "assistant", "content": reply}])


class RaisingRunner:
    def run_sync(self, agent, input, *, context, max_turns):
        raise RuntimeError("boom")


def wait_until(pred, timeout=2.0):
    deadline = time.monotonic() + timeout
    while not pred():
        assert time.monotonic() < deadline, "condition never became true"
        time.sleep(0.01)


def _session(tmp_path, runner, quoted=False, server=None):
    c = WebPaymentCollector()
    server = server or FakeRoadwayServer({RID: make_reservation()}, {RID: EMAIL})
    deps = make_deps(server, tmp_path, NOW, collector=c)
    if quoted:
        assert verify_customer_impl(deps, RID, EMAIL)["outcome"] == "verified"
        assert quote_extension_impl(deps, "2027-06-17T14:00")["outcome"] == "quoted"
    return DemoSession(build_agent(load_settings({})), deps, c, runner=runner)


def _app(tmp_path, runner, quoted=False):
    return DemoApp(lambda: _session(tmp_path, runner, quoted))


def _state(app):
    return app.dispatch("GET", "/api/state?since=0", None)[1]


def _idle(app):
    wait_until(lambda: not _state(app)["busy"])


def test_message_starts_turn_and_reply_appears(tmp_path):
    app = _app(tmp_path, FakeRunner())
    assert app.dispatch("POST", "/api/message", {"text": "hi"}) == (202, {"ok": True})
    _idle(app)
    assert _state(app)["chat"] == [
        {"role": "user", "text": "hi"},
        {"role": "agent", "text": "reply 1"},
    ]


def test_message_while_busy_is_409(tmp_path):
    runner = BlockingRunner()
    app = _app(tmp_path, runner)
    assert app.dispatch("POST", "/api/message", {"text": "a"})[0] == 202
    assert app.dispatch("POST", "/api/message", {"text": "b"}) == (
        409,
        {"error": "busy"},
    )
    runner.release.set()
    _idle(app)


def test_empty_message_is_400(tmp_path):
    app = _app(tmp_path, FakeRunner())
    assert app.dispatch("POST", "/api/message", {"text": "  "})[0] == 400
    assert app.dispatch("POST", "/api/message", {})[0] == 400
    assert app.dispatch("POST", "/api/message", {"text": 5})[0] == 400
    assert app.dispatch("POST", "/api/message", None)[0] == 400


def test_turn_error_reply_and_busy_cleared(tmp_path):
    app = _app(tmp_path, RaisingRunner())
    assert app.dispatch("POST", "/api/message", {"text": "hi"})[0] == 202
    _idle(app)
    assert _state(app)["chat"][-1] == {"role": "agent", "text": TURN_ERROR_REPLY}
    assert app.dispatch("POST", "/api/message", {"text": "again"})[0] == 202
    _idle(app)


def test_state_status_lines(tmp_path):
    app = _app(tmp_path, FakeRunner(), quoted=True)
    status = _state(app)["status"]
    assert status["identity"] == {
        "verification": "verified",
        "reservation_id": RID,
        "email": "s***@example.com",
    }
    quote = status["quote"]
    assert quote["total"] == 100.49
    assert quote["currency"] == "USD"
    assert quote["status"] == "presented"
    assert quote["expires_at"] == (NOW + timedelta(minutes=15)).isoformat()
    assert status["handoffs"] == []


def test_state_since_returns_tail(tmp_path):
    app = _app(tmp_path, FakeRunner(), quoted=True)
    state = _state(app)
    assert state["trace_next"] == len(state["trace"]) > 0
    tail = app.dispatch("GET", f"/api/state?since={state['trace_next']}", None)[1]
    assert tail["trace"] == []
    assert app.dispatch("GET", "/api/state?since=x", None)[0] == 400


def _pay_flow(app, polls=None):
    assert app.dispatch("POST", "/api/message", {"text": "yes"})[0] == 202

    def pending():
        state = _state(app)
        if polls is not None:
            polls.append(json.dumps(state))
        return state["payment"] is not None

    wait_until(pending)


def test_payment_card_flow_in_chat(tmp_path):
    app = _app(tmp_path, PayingRunner(), quoted=True)
    _pay_flow(app)
    cards = [c for c in _state(app)["chat"] if c["role"] == "payment"]
    assert [c["status"] for c in cards] == ["pending"]
    body = {"cvv": "847", "billing_zip": "90210"}
    assert app.dispatch("POST", "/api/payment", body) == (200, {"ok": True})
    _idle(app)
    state = _state(app)
    assert [c["status"] for c in state["chat"] if c["role"] == "payment"] == [
        "submitted"
    ]
    assert state["payment"] is None


def test_payment_without_pending_is_409(tmp_path):
    app = _app(tmp_path, FakeRunner())
    body = {"cvv": "847", "billing_zip": "90210"}
    assert app.dispatch("POST", "/api/payment", body)[0] == 409


def _confirm_outcomes(deps):
    return [
        e["result"].get("outcome")
        for e in deps.state.trace
        if e["event"] == "tool_result" and e["tool"] == "confirm_extension"
    ]


def test_card_details_never_leak(tmp_path):
    app = _app(tmp_path, PayingRunner(), quoted=True)
    session = app.session
    polls: list[str] = []
    _pay_flow(app, polls)
    body = {"cvv": "847", "billing_zip": "90210"}
    assert app.dispatch("POST", "/api/payment", body)[0] == 200
    _idle(app)
    polls.append(json.dumps(_state(app)))
    deps = session.deps
    blobs = [
        json.dumps(session.history),
        json.dumps(deps.state.trace),
        deps.state.logger.path.read_text(),
        *polls,
    ]
    for blob in blobs:
        assert not re.search(r"\b847\b", blob)
        assert not re.search(r"\b90210\b", blob)
    assert _confirm_outcomes(deps) == ["committed"]


def test_reset_during_turn_isolates_old_worker(tmp_path):
    runner = BlockingRunner()
    app = _app(tmp_path, runner)
    old = app.session
    assert app.dispatch("POST", "/api/message", {"text": "a"})[0] == 202
    status, body = app.dispatch("POST", "/api/reset", None)
    assert status == 200 and body["session_id"] != old.deps.state.session_id
    runner.release.set()
    old.thread.join(2)
    state = _state(app)
    assert state["chat"] == [] and state["busy"] is False
    assert state["session_id"] == body["session_id"]


def test_reset_while_payment_pending_declines(tmp_path):
    app = _app(tmp_path, PayingRunner(), quoted=True)
    old = app.session
    _pay_flow(app)
    app.dispatch("POST", "/api/reset", None)
    old.thread.join(2)
    assert not old.thread.is_alive()
    assert _confirm_outcomes(old.deps) == ["customer_declined"]


def test_unknown_route_is_404(tmp_path):
    app = _app(tmp_path, FakeRunner())
    assert app.dispatch("GET", "/nope", None) == (404, {"error": "not found"})


class LateCardRunner:
    def __init__(self):
        self.release = threading.Event()

    def run_sync(self, agent, input, *, context, max_turns):
        self.release.wait(5)
        confirm_extension_impl(context, "q1")
        return FakeResult("done", [*input, {"role": "assistant", "content": "done"}])


def test_reset_before_payment_request_declines_at_once(tmp_path):
    runner = LateCardRunner()
    app = _app(tmp_path, runner, quoted=True)
    old = app.session
    assert app.dispatch("POST", "/api/message", {"text": "yes"})[0] == 202
    app.dispatch("POST", "/api/reset", None)
    runner.release.set()
    old.thread.join(1)
    assert not old.thread.is_alive()
    assert _confirm_outcomes(old.deps) == ["customer_declined"]


class LoopRecordingRunner:
    """Async ``run`` like the SDK's ``Runner``; records the loop each run used."""

    def __init__(self):
        self.loops: list = []

    async def run(self, agent, input, *, context, max_turns):
        self.loops.append(asyncio.get_running_loop())
        return FakeResult("ok", [*input, {"role": "assistant", "content": "ok"}])


def test_shared_loop_runner_uses_one_loop_across_turn_threads():
    # The SDK's model client keeps pooled connections bound to the loop that
    # opened them; a fresh loop per turn thread hangs on a reused connection.
    inner = LoopRecordingRunner()
    runner = SharedLoopRunner(inner)
    results = []
    for _ in range(3):
        thread = threading.Thread(
            target=lambda: results.append(
                runner.run_sync("agent", [], context=None, max_turns=5)
            )
        )
        thread.start()
        thread.join(2)
    assert [r.final_output for r in results] == ["ok"] * 3
    assert len(inner.loops) == 3 and len(set(map(id, inner.loops))) == 1


def test_build_session_defaults_to_shared_loop_runner(monkeypatch):
    monkeypatch.setattr(webui, "build_deps", lambda settings, collector: None)
    first = build_session(load_settings({}))
    second = build_session(load_settings({}))
    assert isinstance(first.runner, SharedLoopRunner)
    assert second.runner is first.runner


# --- demo scenarios ---------------------------------------------------------

_SCENARIO_EMAILS = {
    "RWR-66002030": "david.kim@example.com",
    "RWR-29471835": "sarah.johnson@example.com",
    "RWR-48372915": "marcus.lee@example.com",
}


def _scenario_server(**kwargs):
    return FakeRoadwayServer(
        {rid: make_reservation(reservation_id=rid) for rid in _SCENARIO_EMAILS},
        dict(_SCENARIO_EMAILS),
        **kwargs,
    )


def _scenario_app(tmp_path, server=None):
    server = server or _scenario_server()
    return DemoApp(lambda: _session(tmp_path, FakeRunner(), server=server)), server


def test_scenario_list(tmp_path):
    app, _ = _scenario_app(tmp_path)
    status, body = app.dispatch("GET", "/api/scenarios", None)
    assert status == 200
    assert [s["key"] for s in body["scenarios"]] == [
        "happy",
        "wrong_email",
        "over_limit",
        "overdue",
        "policy",
    ]
    assert all(set(s) == {"key", "label"} for s in body["scenarios"])


def test_over_limit_has_no_yes_and_no_payment():
    assert OVER_LIMIT.payments == [None]
    assert not any(re.search(r"(?i)\byes\b", m) for m in OVER_LIMIT.messages)
    label = next(d.label for d in DEMO_SCENARIOS if d.key == "over_limit")
    assert OVER_LIMIT.extend_by is not None
    assert OVER_LIMIT.extend_by <= timedelta(days=14) or label.startswith("Long")


def test_choose_scenario_resets_and_fills(tmp_path):
    app, _ = _scenario_app(tmp_path)
    app.dispatch("POST", "/api/message", {"text": "hi"})
    _idle(app)
    old_id = _state(app)["session_id"]
    status, body = app.dispatch("POST", "/api/scenarios/happy", None)
    assert status == 200
    assert body["key"] == "happy"
    assert body["lines"][1] == "RWR-66002030, david.kim@example.com"
    assert re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", body["lines"][2])
    assert body["payments"] == [{"cvv": "847", "billing_zip": "90210"}]
    state = _state(app)
    assert state["session_id"] != old_id
    assert state["chat"] == []
    assert state["trace"] == []


def test_choose_policy_needs_no_reservation(tmp_path):
    app, server = _scenario_app(tmp_path)
    status, body = app.dispatch("POST", "/api/scenarios/policy", None)
    assert status == 200
    assert body["lines"] == ["How long is the grace period if I return my car late?"]
    assert not [c for c in server.calls if c["endpoint"] == "reservation"]


def test_choose_unknown_scenario_is_404(tmp_path):
    app, _ = _scenario_app(tmp_path)
    assert app.dispatch("POST", "/api/scenarios/nope", None)[0] == 404


def test_choose_scenario_reservation_failure_is_502(tmp_path):
    import requests

    server = _scenario_server(
        overrides={"reservation": [requests.ConnectionError()] * 10}
    )
    app, _ = _scenario_app(tmp_path, server)
    status, body = app.dispatch("POST", "/api/scenarios/happy", None)
    assert status == 502
    assert "error" in body
    assert body["session_id"] == _state(app)["session_id"]
    state = _state(app)
    assert state["chat"] == [] and state["busy"] is False


def _serve(app):
    server = make_server(app, 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def _http(method, url, data=None):
    req = urllib.request.Request(url, data=data, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.headers, resp.read()
    except urllib.error.HTTPError as err:
        return err.code, err.headers, err.read()


def test_server_serves_page_and_state(tmp_path):
    server, base = _serve(_app(tmp_path, FakeRunner()))
    try:
        status, headers, body = _http("GET", base + "/")
        assert status == 200
        assert headers["Content-Type"].startswith("text/html")
        assert b"Roadway Assistant" in body
        status, headers, body = _http("GET", base + "/api/state")
        assert status == 200
        assert headers["Cache-Control"] == "no-store"
        assert "session_id" in json.loads(body)
    finally:
        server.shutdown()
        server.server_close()


def test_bad_requests(tmp_path):
    server, base = _serve(_app(tmp_path, FakeRunner()))
    try:
        for payload in (b"not json", b"[1]", b'{"txt": "hi"}'):
            status, _, body = _http("POST", base + "/api/message", payload)
            assert status == 400
            assert "error" in json.loads(body)
        status, _, body = _http("GET", base + "/api/nope")
        assert status == 404
        assert "error" in json.loads(body)
    finally:
        server.shutdown()
        server.server_close()


def test_server_binds_loopback_only(tmp_path):
    server = make_server(_app(tmp_path, FakeRunner()), 0)
    try:
        assert server.server_address[0] == "127.0.0.1"
    finally:
        server.server_close()


def _raw(port, head: bytes, body: bytes = b""):
    import http.client

    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.putrequest("POST", "/api/message")
    for line in head.split(b"\n"):
        if line:
            name, value = line.split(b":", 1)
            conn.putheader(name.decode(), value.strip().decode())
    conn.endheaders(body)
    resp = conn.getresponse()
    out = resp.status, json.loads(resp.read())
    conn.close()
    return out


def test_malformed_framing_and_oversize(tmp_path):
    server, _ = _serve(_app(tmp_path, FakeRunner()))
    port = server.server_address[1]
    try:
        for bad in (b"abc", b"-5"):
            status, body = _raw(port, b"Content-Length: " + bad)
            assert (status, body) == (400, {"error": "invalid Content-Length"})
        deep = b"[" * 60000
        status, body = _raw(port, b"Content-Length: %d" % len(deep), deep)
        assert (status, body) == (400, {"error": "invalid JSON"})
        status, body = _raw(port, b"Content-Length: 99999999")
        assert (status, body) == (413, {"error": "request too large"})
    finally:
        server.shutdown()
        server.server_close()


def _page() -> str:
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")


def test_page_accent_only_in_header_accent():
    text = _page().lower()
    assert text.count("#1f5fbf") == 1
    assert "#2f3e4e" in text


def test_page_has_no_external_resources():
    text = _page()
    assert not re.search(r"(src|href)\s*=\s*[\"']?(https?:)?//", text)
    assert "url(http" not in text


def test_page_uses_every_endpoint():
    text = _page()
    for endpoint in (
        "/api/state",
        "/api/message",
        "/api/payment",
        "/api/reset",
        "/api/scenarios",
    ):
        assert endpoint in text
    assert "Handled by code, never shown to the AI" in text
