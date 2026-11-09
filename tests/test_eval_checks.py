"""Eval scenario checks and the runner's exit code, on synthetic traces only."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from evals import run_evals
from evals.scenarios import SCENARIOS, commits, new_return_local

SPEC_NAMES = {
    "preferred_happy",
    "standard_happy",
    "due_soon",
    "overdue",
    "availability_timeout",
    "declines_quote",
    "wrong_email_twice",
    "grace_question",
    "cancel_request",
    "tiny_extension",
}


def _scenario(name: str):
    return next(s for s in SCENARIOS if s.name == name)


def _tool_result(tool: str, **result) -> dict:
    return {"event": "tool_result", "tool": tool, "result": result}


AUTHORITY = {
    "kb_ext_01": "official-policy",
    "kb_fee_02": "official-policy",
    "kb_fee_01": "legacy",
    "kb_help_01": "help-center",
}


def _kb_search(*ids: str) -> dict:
    return {
        "event": "kb_search",
        "query": "grace period",
        "fallback": False,
        "results": [{"id": i, "score": 0.5, "authority": AUTHORITY[i]} for i in ids],
    }


def test_scenarios_named_as_spec():
    assert {s.name for s in SCENARIOS} == SPEC_NAMES


def test_commits_counts_committed_only():
    trace = [
        _tool_result("confirm_extension", outcome="committed"),
        _tool_result("confirm_extension", outcome="payment_failed"),
    ]
    assert commits(trace) == 1


def test_grace_check_ranking():
    check = _scenario("grace_question").check
    fee_first = [_kb_search("kb_fee_01", "kb_ext_01")]
    help_first = [_kb_search("kb_help_01", "kb_fee_02")]
    ext_first = [_kb_search("kb_ext_01", "kb_fee_01")]
    other_official_first = [_kb_search("kb_fee_02", "kb_fee_01")]
    answer = "The grace period is 30 minutes."

    assert check(fee_first, answer)[0] == "fail"
    assert check(help_first, answer)[0] == "fail"
    assert check([], answer)[0] == "fail"
    assert check(ext_first, answer)[0] == "pass"
    assert check(other_official_first, answer)[0] == "pass"
    assert check(ext_first, "The grace period is 30 minutes, not 2 hours.")[0] == "fail"


def test_availability_skip_rule():
    check = _scenario("availability_timeout").check
    available = [
        {
            "event": "guardrail",
            "check": "availability",
            "verdict": "allow",
            "reason": None,
        }
    ]
    handed_off = [
        {
            "event": "guardrail",
            "check": "availability",
            "verdict": "escalate",
            "reason": "availability_unconfirmed",
        },
        {"event": "handoff", "reason": "availability_unconfirmed", "reference": "HO-1"},
    ]
    assert check(available, "")[0] == "skipped (precondition)"
    assert check(handed_off, "")[0] == "pass"


def test_new_return_local_uses_later_of_now():
    reservation = {
        "return_location": {"code": "LAX"},
        "dates": {
            "pickup_datetime": "2027-06-01T10:00:00-07:00",
            "current_return_datetime": "2027-06-05T14:00:00-07:00",
        },
    }
    now = datetime(2027, 6, 10, 17, 30, 45, tzinfo=timezone.utc)  # 10:30 PDT
    assert new_return_local(reservation, now, timedelta(days=2)) == "2027-06-12T10:30"


def test_run_evals_exit_code(monkeypatch, tmp_path):
    monkeypatch.setattr(run_evals, "load_settings", lambda: _settings(tmp_path))
    statuses = iter(["pass", "skipped (precondition)"])

    def fake_run(settings, scenario, run_index):
        return {
            "scenario": scenario.name,
            "run": run_index,
            "status": next(statuses, "pass"),
            "detail": "",
            "session_id": "s",
        }

    monkeypatch.setattr(run_evals, "run_scenario", fake_run)
    assert run_evals.main([]) == 0

    def failing_run(settings, scenario, run_index):
        row = fake_run(settings, scenario, run_index)
        if scenario.name == "overdue":
            row["status"] = "fail"
        return row

    monkeypatch.setattr(run_evals, "run_scenario", failing_run)
    assert run_evals.main([]) == 1


def _settings(tmp_path):
    from config import Settings

    return Settings(log_dir=str(tmp_path / "logs"))
