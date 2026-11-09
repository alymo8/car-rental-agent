"""Local web demo console for the rental-extension agent.

Card details (CVV, billing ZIP) go only from the request handler to the waiting
``collect`` call; they are never stored in the chat, trace, logs, model history
or any state the browser can read.
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from agents import Agent, Runner
from dotenv import load_dotenv

from agent import build_agent, build_deps, respond
from config import Settings, load_settings
from evals.scenarios import SCENARIOS, Scenario, new_return_local
from payment import CVV_RE, ZIP_RE, PaymentDetails
from telemetry import mask_email
from tools import ToolDeps

PAYMENT_TIMEOUT_S = 300.0
STATIC_DIR = Path(__file__).resolve().parent / "static"
DEFAULT_PORT = 8765
MAX_BODY_BYTES = 65536

_PUBLIC_KEYS = ("amount", "currency", "card_type", "last_four", "description")


class WebPaymentCollector:
    """A ``PaymentCollector`` that blocks the tool's thread until the browser answers."""

    def __init__(
        self,
        timeout: float = PAYMENT_TIMEOUT_S,
        on_request: Callable[[dict], None] | None = None,
    ):
        self._timeout = timeout
        self._on_request = on_request
        self._lock = threading.Lock()
        self._item: dict | None = None
        self._event: threading.Event | None = None
        self._answered = False
        self._result: PaymentDetails | None = None
        self._status = "cancelled"
        self._closed = False

    def collect(
        self,
        amount: float,
        currency: str,
        card_type: str,
        last_four: str,
        description: str,
    ) -> PaymentDetails | None:
        item = {
            "role": "payment",
            "amount": float(amount),
            "currency": currency,
            "card_type": card_type or None,
            "last_four": last_four or None,
            "description": description,
            "status": "pending",
        }
        event = threading.Event()
        with self._lock:
            if self._closed:
                item["status"] = "cancelled"
                return None
            self._item = item
            self._event = event
            self._answered = False
            self._result = None
            self._status = "cancelled"
        if self._on_request is not None:
            self._on_request(item)
        event.wait(self._timeout)
        with self._lock:
            if self._answered:
                result, status = self._result, self._status
            else:
                result, status = None, "expired"
                self._answered = True
            self._item = None
            self._event = None
            self._result = None
            item["status"] = status
        return result

    def pending(self) -> dict | None:
        with self._lock:
            if self._item is None or self._answered:
                return None
            return {key: self._item[key] for key in _PUBLIC_KEYS}

    def _resolve(self, result: PaymentDetails | None, status: str) -> bool:
        """Resolve the pending wait once; caller holds the lock."""
        if self._item is None or self._answered or self._event is None:
            return False
        self._answered = True
        self._result = result
        self._status = status
        self._event.set()
        return True

    def answer(
        self,
        cvv: str | None = None,
        billing_zip: str | None = None,
        cancel: bool = False,
    ) -> bool:
        cvv = (cvv or "").strip()
        billing_zip = (billing_zip or "").strip()
        details = None
        if not cancel and CVV_RE.match(cvv) and ZIP_RE.match(billing_zip):
            details = PaymentDetails(cvv=cvv, billing_zip=billing_zip)
        with self._lock:
            return self._resolve(
                details, "submitted" if details is not None else "cancelled"
            )

    def close(self) -> None:
        """Refuse future requests and decline a waiting one."""
        with self._lock:
            self._closed = True
            self._resolve(None, "cancelled")

    def cancel_pending(self) -> None:
        with self._lock:
            self._resolve(None, "cancelled")


class SharedLoopRunner:
    """Runs every agent turn on one long-lived event loop, whatever thread asks.

    ``Runner.run_sync`` uses the calling thread's own event loop, so a worker
    thread per turn means a new loop per turn. The SDK's model client is shared
    and keeps pooled connections bound to the loop that opened them; a turn
    sent within the pool's keep-alive window then reuses a connection whose
    loop no longer runs, and hangs. Function tools still run in worker threads
    (the SDK calls sync tools with ``asyncio.to_thread``), so a pending payment
    does not block the loop.
    """

    def __init__(self, runner=Runner):
        self._runner = runner
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock = threading.Lock()

    def _get_loop(self) -> asyncio.AbstractEventLoop:
        with self._lock:
            if self._loop is None:
                loop = asyncio.new_event_loop()
                threading.Thread(
                    target=loop.run_forever, name="agent-loop", daemon=True
                ).start()
                self._loop = loop
            return self._loop

    def run_sync(self, agent, input, *, context, max_turns):
        coro = self._runner.run(agent, input, context=context, max_turns=max_turns)
        return asyncio.run_coroutine_threadsafe(coro, self._get_loop()).result()


SHARED_RUNNER = SharedLoopRunner()


class DemoSession:
    """One conversation: agent, deps, chat transcript and the turn worker."""

    def __init__(
        self,
        agent: Agent[ToolDeps],
        deps: ToolDeps,
        collector: WebPaymentCollector,
        runner=Runner,
    ):
        self.agent = agent
        self.deps = deps
        self.collector = collector
        self.runner = runner
        self.thread: threading.Thread | None = None
        self.chat: list[dict] = []
        self.history: list = []
        self.busy = False
        self.closed = False
        self._lock = threading.Lock()
        collector._on_request = self._on_payment_request

    def _on_payment_request(self, item: dict) -> None:
        with self._lock:
            self.chat.append(item)

    def start_turn(self, text: str) -> bool:
        with self._lock:
            if self.busy or self.closed:
                return False
            self.busy = True
            self.chat.append({"role": "user", "text": text})
            history = self.history
            self.thread = threading.Thread(
                target=self._work, args=(text, history), daemon=True
            )
            self.thread.start()
        return True

    def _work(self, text: str, history: list) -> None:
        reply, new_history = respond(
            self.agent, self.deps, history, text, runner=self.runner
        )
        with self._lock:
            if self.closed:
                return
            self.history = new_history
            self.chat.append({"role": "agent", "text": reply})
            self.busy = False

    def state(self, since: int = 0) -> dict:
        st = self.deps.state
        with self._lock:
            chat = [dict(item) for item in self.chat]
            busy = self.busy
        quote = st.latest_quote()
        quote_view = None
        if quote is not None:
            ttl = timedelta(minutes=self.deps.settings.quote_ttl_minutes)
            quote_view = {
                "total": quote.charges.get("total_charged"),
                "currency": quote.charges.get("currency"),
                "status": quote.status,
                "expires_at": (quote.created_at + ttl).isoformat(),
            }
        trace = list(st.trace)
        return {
            "session_id": st.session_id,
            "busy": busy,
            "chat": chat,
            "payment": self.collector.pending(),
            "status": {
                "identity": {
                    "verification": st.verification,
                    "reservation_id": st.reservation_id,
                    "email": mask_email(st.email) if st.email else None,
                },
                "quote": quote_view,
                "handoffs": [
                    {"reason": reason, "reference": ref}
                    for reason, ref in list(st.handoffs.items())
                ],
            },
            "trace": trace[since:],
            "trace_next": len(trace),
        }

    def close(self) -> None:
        with self._lock:
            self.closed = True
        self.collector.close()


@dataclass(frozen=True)
class DemoScenario:
    key: str
    label: str
    scenario: Scenario


def _named(name: str) -> Scenario:
    for scenario in SCENARIOS:
        if scenario.name == name:
            return scenario
    raise LookupError(f"eval scenario {name!r} not found")


# Probe (live quotes): RWR-29471835 + 10 days quoted 502.44, + 12 days 602.93.
# 12 days gives margin over the $500 threshold. The script stops after the new
# return time and its payment is None, so it can never lead to a charge.
OVER_LIMIT = Scenario(
    "over_limit",
    "RWR-29471835",
    "sarah.johnson@example.com",
    [
        "I'd like to extend my rental.",
        "{rid}, {email}",
        "Please change the return to {new_local}.",
    ],
    check=lambda trace, replies: ("pass", ""),
    extend_by=timedelta(days=12),
    payments=[None],
)

DEMO_SCENARIOS: list[DemoScenario] = [
    DemoScenario("happy", "Happy path", _named("standard_happy")),
    DemoScenario("wrong_email", "Wrong email", _named("wrong_email_twice")),
    DemoScenario("over_limit", "Over $500 → human", OVER_LIMIT),
    DemoScenario("overdue", "Overdue → human", _named("overdue")),
    DemoScenario("policy", "Policy question", _named("grace_question")),
]


def fill_scenario(demo: DemoScenario, session: DemoSession) -> dict:
    """The scripted lines with ``{rid}``, ``{email}``, ``{new_local}`` filled in.

    The reservation read goes through a copy of the client with no attempt
    hook, so it reaches neither the session trace nor the chat.
    """
    sc = demo.scenario
    new_local = ""
    if sc.extend_by is not None:
        client = copy.copy(session.deps.client)
        client.on_attempt = lambda attempt: None
        reservation = client.get_reservation(sc.reservation_id)
        new_local = new_return_local(reservation, session.deps.now(), sc.extend_by)
    values = {
        "rid": sc.reservation_id or "",
        "email": sc.email or "",
        "new_local": new_local,
    }
    return {
        "key": demo.key,
        "label": demo.label,
        "lines": [line.format(**values) for line in sc.messages],
        "payments": [
            None if p is None else {"cvv": p.cvv, "billing_zip": p.billing_zip}
            for p in sc.payments
        ],
    }


class DemoApp:
    """Routes API requests to the current session; reset swaps in a new one."""

    def __init__(self, make_session: Callable[[], DemoSession]):
        self._make_session = make_session
        self._lock = threading.Lock()
        self.session = make_session()

    def reset(self) -> DemoSession:
        with self._lock:
            self.session.close()
            self.session = self._make_session()
            return self.session

    def dispatch(self, method: str, path: str, body: dict | None) -> tuple[int, dict]:
        parts = urlsplit(path)
        route = (method, parts.path)
        session = self.session
        if route == ("GET", "/api/state"):
            raw = parse_qs(parts.query).get("since", ["0"])[0]
            try:
                since = int(raw)
            except ValueError:
                return 400, {"error": "since must be an integer"}
            return 200, session.state(max(since, 0))
        if route == ("POST", "/api/message"):
            text = (body or {}).get("text")
            if not isinstance(text, str) or not text.strip():
                return 400, {"error": "text is required"}
            if not session.start_turn(text.strip()):
                return 409, {"error": "busy"}
            return 202, {"ok": True}
        if route == ("POST", "/api/payment"):
            data = body or {}
            ok = session.collector.answer(
                cvv=_as_text(data.get("cvv")),
                billing_zip=_as_text(data.get("billing_zip")),
                cancel=data.get("cancel") is True,
            )
            if not ok:
                return 409, {"error": "no payment pending"}
            return 200, {"ok": True}
        if route == ("POST", "/api/reset"):
            return 200, {"session_id": self.reset().deps.state.session_id}
        if route == ("GET", "/api/scenarios"):
            return 200, {
                "scenarios": [{"key": d.key, "label": d.label} for d in DEMO_SCENARIOS]
            }
        if method == "POST" and parts.path.startswith("/api/scenarios/"):
            key = parts.path[len("/api/scenarios/") :]
            demo = next((d for d in DEMO_SCENARIOS if d.key == key), None)
            if demo is None:
                return 404, {"error": "unknown scenario"}
            fresh = self.reset()
            try:
                return 200, fill_scenario(demo, fresh)
            except Exception as err:  # noqa: BLE001 - any read failure becomes a 502
                return 502, {
                    "error": f"Could not load the reservation: {type(err).__name__}",
                    "session_id": fresh.deps.state.session_id,
                }
        return 404, {"error": "not found"}


def _as_text(value) -> str | None:
    return value if isinstance(value, str) else None


def make_handler(app: DemoApp) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, payload: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)

        def _send_json(self, status: int, body: dict) -> None:
            self._send(status, json.dumps(body).encode(), "application/json")

        def _api(self, method: str) -> None:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if length < 0:
                return self._send_json(400, {"error": "invalid Content-Length"})
            if length > MAX_BODY_BYTES:
                self.close_connection = True
                return self._send_json(413, {"error": "request too large"})
            raw = self.rfile.read(length) if length > 0 else b""
            body = None
            if raw.strip():
                try:
                    body = json.loads(raw)
                except (ValueError, RecursionError):
                    return self._send_json(400, {"error": "invalid JSON"})
                if not isinstance(body, dict):
                    return self._send_json(400, {"error": "invalid JSON"})
            status, reply = app.dispatch(method, self.path, body)
            self._send_json(status, reply)

        def do_GET(self) -> None:
            if self.path.startswith("/api/"):
                return self._api("GET")
            if self.path.split("?", 1)[0] == "/":
                page = STATIC_DIR / "index.html"
                if page.is_file():
                    return self._send(
                        200, page.read_bytes(), "text/html; charset=utf-8"
                    )
            self._send_json(404, {"error": "not found"})

        def do_POST(self) -> None:
            if self.path.startswith("/api/"):
                return self._api("POST")
            self._send_json(404, {"error": "not found"})

        def log_message(self, format, *args) -> None:
            pass

    return Handler


def make_server(app: DemoApp, port: int) -> ThreadingHTTPServer:
    return ThreadingHTTPServer(("127.0.0.1", port), make_handler(app))


def build_session(settings: Settings, runner=SHARED_RUNNER) -> DemoSession:
    c = WebPaymentCollector()
    deps = build_deps(settings, c)
    return DemoSession(build_agent(settings), deps, c, runner)


def main() -> None:
    load_dotenv()
    missing = [
        k for k in ("OPENAI_API_KEY", "ROADWAY_API_KEY") if not os.environ.get(k)
    ]
    if missing:
        print(f"Set {' and '.join(missing)} in your .env first (see env.example).")
        return
    settings = load_settings()
    port = int(os.environ.get("WEBUI_PORT") or DEFAULT_PORT)
    app = DemoApp(lambda: build_session(settings))
    server = make_server(app, port)
    print(
        f"Roadway demo console: http://127.0.0.1:{port}  (Ctrl+C to stop)", flush=True
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.session.close()
        server.server_close()


if __name__ == "__main__":
    main()
