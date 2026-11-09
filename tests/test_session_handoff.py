import json
from datetime import datetime, timezone
from pathlib import Path

from handoff import HandoffWriter
from session import ConversationState, Quote
from telemetry import SessionLogger


def _quote(token: str) -> Quote:
    now = datetime(2027, 6, 15, 12, 0, tzinfo=timezone.utc)
    return Quote(
        token=token,
        new_return_utc=now,
        new_return_local="2027-06-17 14:00 PDT",
        charges={"total": 120.0},
        created_at=now,
    )


def test_new_quote_supersedes_old():
    state = ConversationState(session_id="s1")
    q1, q2 = _quote("q1"), _quote("q2")
    state.add_quote(q1)
    state.add_quote(q2)
    assert q1.status == "superseded"
    assert state.latest_quote() is q2


def test_record_appends_redacted_trace(tmp_path):
    logger = SessionLogger("s1", tmp_path)
    state = ConversationState(session_id="s1", logger=logger)
    state.record("tool_call", args={"email": "a.b@example.com"})
    assert state.trace[-1]["args"]["email"] == "a***@example.com"
    assert len(logger.path.read_text().strip().splitlines()) == 1


def test_handoff_written(tmp_path):
    state = ConversationState(
        session_id="s1", reservation_id="R1", email="david.kim@example.com"
    )
    for i in range(12):
        state.record("tool_call", n=i)
    result = HandoffWriter(tmp_path).open(state, "overdue_rental", "summary")
    path = tmp_path / "handoffs" / "s1-1.json"
    assert path.exists()
    assert result["handoff_reference"] == "HO-s1-1"
    assert result["already_open"] is False
    rec = json.loads(path.read_text())
    assert rec["reference"] == "HO-s1-1"
    assert rec["email_masked"] == "d***@example.com"
    assert len(rec["recent_events"]) <= 10


def test_handoff_dedup_per_reason(tmp_path):
    state = ConversationState(session_id="s1")
    writer = HandoffWriter(tmp_path)
    first = writer.open(state, "overdue_rental", "summary")
    second = writer.open(state, "overdue_rental", "again")
    assert second["already_open"] is True
    assert second["handoff_reference"] == first["handoff_reference"]
    assert len(list((tmp_path / "handoffs").iterdir())) == 1
    third = writer.open(state, "high_value", "other")
    assert third["handoff_reference"] == "HO-s1-2"
    assert (tmp_path / "handoffs" / "s1-2.json").exists()


def test_handoff_never_contains_card_fields(tmp_path):
    state = ConversationState(session_id="s1")
    state.trace.append({"event": "tool_call", "payment": {"cvv": "847"}})
    writer = HandoffWriter(tmp_path)
    result = writer.open(state, "payment_failed", "summary")
    assert "847" not in Path(result["path"]).read_text()
