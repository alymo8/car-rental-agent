"""Agent definition and CLI chat loop. No network: the runner is a fake."""

from __future__ import annotations

from datetime import datetime, timezone

from fakes import FakeRoadwayServer, make_deps

from agent import INSTRUCTIONS, build_agent, chat_loop, guarded, run_turn
from config import load_settings

NOW = datetime(2027, 6, 14, 17, 0, tzinfo=timezone.utc)


class FakeResult:
    def __init__(self, final_output: str, input_list: list):
        self.final_output = final_output
        self._input_list = input_list

    def to_input_list(self) -> list:
        return list(self._input_list)


class FakeRunner:
    def __init__(self):
        self.calls: list[dict] = []

    def run_sync(self, agent, input, *, context, max_turns):
        self.calls.append(
            {
                "agent": agent,
                "input": list(input),
                "context": context,
                "max_turns": max_turns,
            }
        )
        reply = f"reply {len(self.calls)}"
        return FakeResult(reply, [*input, {"role": "assistant", "content": reply}])


def _deps(tmp_path):
    return make_deps(FakeRoadwayServer({}, {}), tmp_path, NOW)


def test_agent_has_exactly_five_tools():
    agent = build_agent(load_settings({}))
    assert {t.name for t in agent.tools} == {
        "verify_customer",
        "quote_extension",
        "confirm_extension",
        "search_knowledge_base",
        "escalate_to_human",
    }


def test_instructions_cover_rules():
    for phrase in (
        "kb_ext_01",
        "30 minutes",
        "explicit yes",
        "card details",
        "escalate_to_human",
        "customer_insists",
        "search_knowledge_base again",
        "extension policy grace period",
        "Cite only articles whose text states",
        "never cite a legacy article",
    ):
        assert phrase in INSTRUCTIONS, phrase


def test_chat_loop_logs_turns_and_exits(tmp_path):
    deps = _deps(tmp_path)
    inputs = iter(["hi", "", "quit"])
    printed: list[str] = []
    runner = FakeRunner()
    chat_loop(
        build_agent(load_settings({})),
        deps,
        input_fn=lambda prompt="": next(inputs),
        print_fn=printed.append,
        runner=runner,
    )
    assert len(runner.calls) == 1
    events = [r["event"] for r in deps.state.trace]
    assert "turn_user" in events
    assert "turn_agent" in events
    assert printed[0].startswith("Session ")


def test_chat_loop_eof(tmp_path):
    def raise_eof(prompt=""):
        raise EOFError

    runner = FakeRunner()
    chat_loop(
        build_agent(load_settings({})),
        _deps(tmp_path),
        input_fn=raise_eof,
        print_fn=lambda *_: None,
        runner=runner,
    )
    assert runner.calls == []


def test_history_carried(tmp_path):
    deps = _deps(tmp_path)
    agent = build_agent(load_settings({}))
    runner = FakeRunner()
    reply, history = run_turn(agent, deps, [], "hello", runner=runner)
    assert reply == "reply 1"
    run_turn(agent, deps, history, "again", runner=runner)
    assert runner.calls[1]["input"] == [
        *history,
        {"role": "user", "content": "again"},
    ]
    assert runner.calls[1]["context"] is deps
    assert runner.calls[1]["max_turns"] == 20


def test_guarded_turns_exceptions_into_system_unavailable(tmp_path):
    deps = _deps(tmp_path)

    def boom():
        raise RuntimeError("unexpected")

    assert guarded(deps, "quote_extension", boom) == {"outcome": "system_unavailable"}
    errors = [r for r in deps.state.trace if r["event"] == "error"]
    assert errors and errors[0]["level"] == "error"
    assert errors[0]["tool"] == "quote_extension"


def test_instructions_cover_final_review_rules():
    for phrase in (
        "already_committed",
        "system_unavailable from confirm_extension",
        "do not say whether a charge happened",
        "the card on file",
    ):
        assert phrase in INSTRUCTIONS, phrase


RID = "RWR-29471835"
EMAIL = "sarah.johnson@example.com"


class CrashAfterToolRunner:
    """Runs `tool(deps)` inside the turn, then raises as a failed model call would."""

    def __init__(self, tool):
        self.tool = tool

    def run_sync(self, agent, input, *, context, max_turns):
        self.tool(context)
        raise RuntimeError("model call failed")


def _quoted_deps(tmp_path, **server_kw):
    from fakes import make_reservation

    from tools import quote_extension_impl, verify_customer_impl

    server = FakeRoadwayServer({RID: make_reservation()}, {RID: EMAIL}, **server_kw)
    deps = make_deps(server, tmp_path, NOW)
    assert verify_customer_impl(deps, RID, EMAIL)["outcome"] == "verified"
    assert quote_extension_impl(deps, "2027-06-17T14:00")["outcome"] == "quoted"
    return server, deps


def _chat_once(deps, runner):
    inputs = iter(["yes", "quit"])
    printed: list[str] = []
    chat_loop(
        build_agent(load_settings({})),
        deps,
        input_fn=lambda prompt="": next(inputs),
        print_fn=printed.append,
        runner=runner,
    )
    return printed[-1]


def test_chat_loop_reports_commit_from_failed_turn(tmp_path):
    from tools import confirm_extension_impl

    _, deps = _quoted_deps(tmp_path)
    runner = CrashAfterToolRunner(lambda d: confirm_extension_impl(d, "q1"))
    reply = _chat_once(deps, runner)
    assert "EXT-000001" in reply
    assert "2027-06-17T14:00" in reply
    assert "say that again" not in reply


def test_chat_loop_reports_unknown_charge_from_failed_turn(tmp_path):
    from fakes import FakeResponse

    from tools import confirm_extension_impl

    unavailable = FakeResponse(
        503, {"error": {"code": "SERVICE_UNAVAILABLE", "message": "down"}}
    )
    _, deps = _quoted_deps(tmp_path, overrides={"extend_payment": [unavailable] * 3})
    runner = CrashAfterToolRunner(lambda d: confirm_extension_impl(d, "q1"))
    reply = _chat_once(deps, runner)
    reference = deps.state.handoffs["charge_status_unknown"]
    assert reference in reply
    assert "could not confirm" in reply
    assert "say that again" not in reply


def test_chat_loop_plain_error_reply_without_charge(tmp_path):
    _, deps = _quoted_deps(tmp_path)
    reply = _chat_once(deps, CrashAfterToolRunner(lambda d: None))
    assert "say that again" in reply


def test_respond_returns_reply_and_history(tmp_path):
    from agent import respond

    deps = _deps(tmp_path)
    reply, history = respond(
        build_agent(load_settings({})), deps, [], "hi", runner=FakeRunner()
    )
    assert reply == "reply 1"
    assert history[-1] == {"role": "assistant", "content": "reply 1"}


def test_respond_error_keeps_charge_outcome_in_history(tmp_path):
    from agent import respond
    from tools import confirm_extension_impl

    _, deps = _quoted_deps(tmp_path)
    runner = CrashAfterToolRunner(lambda d: confirm_extension_impl(d, "q1"))
    reply, history = respond(
        build_agent(load_settings({})), deps, [], "yes", runner=runner
    )
    assert "EXT-000001" in reply
    assert history == [
        {"role": "user", "content": "yes"},
        {"role": "assistant", "content": reply},
    ]


def test_respond_plain_error(tmp_path):
    from agent import TURN_ERROR_REPLY, respond

    _, deps = _quoted_deps(tmp_path)
    reply, history = respond(
        build_agent(load_settings({})),
        deps,
        [],
        "hi",
        runner=CrashAfterToolRunner(lambda d: None),
    )
    assert reply == TURN_ERROR_REPLY and history == []
