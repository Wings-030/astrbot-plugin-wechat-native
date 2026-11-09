import sqlite3
from datetime import datetime, timedelta

from conftest import module


def test_statistics_deduplicate_and_preserve_outcomes_across_days(tmp_path, monkeypatch):
    state_module = module("state")
    now = datetime.now().astimezone()

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    monkeypatch.setattr(state_module, "datetime", Clock, raising=False)
    path = tmp_path / "state.sqlite3"
    state = state_module.StateStore(path)
    try:
        assert state.accept("1", "m1", "p", "1", "queued")
        assert not state.accept("2", "m1", "p", "1", "queued")
        state.mark_message("m1", "replied")
        state.mark_message("m1", "replied")
        state.accept("3", "m2", "p", "2", "queued")
        state.mark_message("m2", "replied")
        state.mark_message("m2", "failed")
        state.accept("4", "m3", "p", "3", "failed")
        state.accept("5", "m4", "p", "4", "queued")
        state.accept("6", "outgoing", "p", "5", "ignored", received=False)
        first_day = now.date().isoformat()
        now += timedelta(days=1)
        state.accept("7", "m5", "p", "6", "ignored")
        result = state_module.read_statistics(path)
        assert len(result["days"]) == 7
        previous = result["days"][-2]
        assert previous == {
            "date": first_day,
            "received": 4,
            "replied": 1,
            "failed": 1,
            "partial": 1,
        }
        assert result["today"]["received"] == 1
        assert result["pending_review"] == 1
        assert state.message_status("m4") == "queued", "panel must not run startup recovery"
        assert result["days"][0]["received"] is None
    finally:
        state.close()
    state = state_module.StateStore(path)
    try:
        result = state_module.read_statistics(path)
        assert result["days"][-2]["received"] == 4
        assert result["days"][-2]["replied"] == 1
        assert result["pending_review"] == 2
    finally:
        state.close()


def test_statistics_missing_legacy_and_independent_journals(tmp_path):
    state_module = module("state")
    missing = tmp_path / "missing.sqlite3"
    assert state_module.read_statistics(missing)["available"] is False
    assert not missing.exists()
    legacy = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(legacy) as db:
        db.execute("CREATE TABLE messages (id TEXT PRIMARY KEY, status TEXT)")
        db.execute("INSERT INTO messages VALUES ('old', 'queued')")
    before = legacy.read_bytes()
    assert state_module.read_statistics(legacy)["available"] is False
    assert legacy.read_bytes() == before
    corrupt = tmp_path / "corrupt.sqlite3"
    corrupt.write_bytes(b"not a sqlite database")
    unavailable = state_module.read_statistics(corrupt)
    assert unavailable["available"] is False
    assert unavailable["reason"] == "无法读取统计，请检查机器人状态库是否可用"
    assert corrupt.read_bytes() == b"not a sqlite database"
    a = state_module.StateStore(tmp_path / "a.sqlite3")
    b = state_module.StateStore(tmp_path / "b.sqlite3")
    try:
        a.accept("1", "same-id", "p", "1", "replied")
        assert state_module.read_statistics(tmp_path / "a.sqlite3")["today"]["received"] == 1
        assert state_module.read_statistics(tmp_path / "b.sqlite3")["today"]["received"] == 0
    finally:
        a.close()
        b.close()


def test_upgrade_keeps_old_messages_out_of_new_daily_totals(tmp_path):
    state_module = module("state")
    path = tmp_path / "state.sqlite3"
    state = state_module.StateStore(path)
    state.accept("1", "old", "p", "1", "replied")
    state.db.execute("DROP TABLE message_days")
    state.db.execute("DELETE FROM settings WHERE key='statistics_since'")
    state.db.commit()
    state.close()
    state = state_module.StateStore(path)
    try:
        assert state_module.read_statistics(path)["today"]["received"] == 0
        assert not state.accept("2", "old", "p", "1", "replied")
        state.accept("3", "new", "p", "2", "ignored")
        result = state_module.read_statistics(path)
        assert result["today"]["received"] == 1
        assert result["today"]["replied"] == 0
    finally:
        state.close()
