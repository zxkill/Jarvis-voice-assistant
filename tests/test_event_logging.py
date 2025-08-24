import json
from core import events as core_events
from core.events import Event
import memory.db as db
from memory import event_logger
from memory.event_logger import setup_event_logging
from memory import writer
from emotion.state import Emotion


def test_event_logging_throttles(monkeypatch, tmp_path):
    db_file = tmp_path / "memory.sqlite3"
    monkeypatch.setattr(db, "DB_PATH", db_file)

    # control time
    now = [10.0]
    def fake_time():
        return now[0]
    monkeypatch.setattr(event_logger.time, "time", fake_time)
    monkeypatch.setattr(writer.time, "time", fake_time)
    monkeypatch.setattr(db.time, "time", fake_time)

    core_events._subscribers.clear()
    core_events._global_subscribers.clear()
    event_logger._last_ts.clear()
    setup_event_logging()

    core_events.publish(Event(kind="ping", attrs={"n": 1}))
    now[0] = 10.0 + event_logger.THROTTLE_SECONDS / 2
    core_events.publish(Event(kind="ping", attrs={"n": 2}))  # игнорируется
    now[0] = 10.0 + event_logger.THROTTLE_SECONDS + 0.1
    core_events.publish(Event(kind="ping", attrs={"n": 3}))

    with db.get_connection() as conn:
        rows = conn.execute("SELECT payload FROM events ORDER BY ts").fetchall()
        assert len(rows) == 2
        payloads = [json.loads(r["payload"]) for r in rows]
        assert payloads[0]["n"] == 1
        assert payloads[1]["n"] == 3


def test_rotation_removes_old_events(monkeypatch, tmp_path):
    db_file = tmp_path / "memory.sqlite3"
    monkeypatch.setattr(db, "DB_PATH", db_file)

    now = 1_000_000
    monkeypatch.setattr(db.time, "time", lambda: now)

    core_events._subscribers.clear()
    core_events._global_subscribers.clear()

    with db.get_connection() as conn:
        conn.execute(
            "INSERT INTO events (ts, event_type) VALUES (?, ?)",
            (now - db.RETENTION_SECONDS - 1, "old"),
        )
        conn.execute(
            "INSERT INTO events (ts, event_type) VALUES (?, ?)",
            (now, "new"),
        )
        conn.commit()

    with db.get_connection() as conn:
        types = [row["event_type"] for row in conn.execute("SELECT event_type FROM events").fetchall()]
        assert types == ["new"]


def test_write_event_serializes_enum(monkeypatch, tmp_path):
    db_file = tmp_path / "memory.sqlite3"
    monkeypatch.setattr(db, "DB_PATH", db_file)

    event_id = writer.write_event("emotion_changed", {"emotion": Emotion.HAPPY})
    with db.get_connection() as conn:
        row = conn.execute("SELECT payload FROM events WHERE id=?", (event_id,)).fetchone()
    payload = json.loads(row["payload"])
    assert payload["emotion"] == Emotion.HAPPY.value


def test_speech_recognized_logged(monkeypatch, tmp_path):
    db_file = tmp_path / "memory.sqlite3"
    monkeypatch.setattr(db, "DB_PATH", db_file)

    core_events._subscribers.clear()
    core_events._global_subscribers.clear()
    event_logger._last_ts.clear()
    setup_event_logging()

    core_events.publish(Event(kind="speech.recognized", attrs={"text": "привет"}))

    with db.get_connection() as conn:
        row = conn.execute("SELECT event_type, payload FROM events").fetchone()
    assert row["event_type"] == "speech.recognized"
    assert json.loads(row["payload"])["text"] == "привет"


def test_user_query_started_logged(monkeypatch, tmp_path):
    db_file = tmp_path / "memory.sqlite3"
    monkeypatch.setattr(db, "DB_PATH", db_file)

    core_events._subscribers.clear()
    core_events._global_subscribers.clear()
    event_logger._last_ts.clear()
    setup_event_logging()

    core_events.publish(Event(kind="user_query_started", attrs={"text": "привет"}))

    with db.get_connection() as conn:
        row = conn.execute("SELECT event_type, payload FROM events").fetchone()
    assert row["event_type"] == "user_query_started"
    assert json.loads(row["payload"])["text"] == "привет"
