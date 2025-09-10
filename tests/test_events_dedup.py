import datetime as dt
from cryptography.fernet import Fernet

import analysis.proactivity as proactivity
import datetime as dt
from cryptography.fernet import Fernet
from core.events import Event
from memory import events as user_events
from memory import db as memory_db, writer


def test_event_greeting_skipped(monkeypatch, tmp_path):
    """Поздравления задолго до события игнорируются."""

    monkeypatch.setattr(memory_db, "DB_PATH", tmp_path / "memory.sqlite3")
    monkeypatch.setenv("JARVIS_DB_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(
        proactivity,
        "load_playbook",
        lambda: {"context_hint": {"prompt": "С днём рождения!"}},
    )
    monkeypatch.setattr(user_events, "load_event_date", lambda k: dt.date(1988, 5, 7))
    monkeypatch.setattr(user_events, "_today", lambda: dt.date(2024, 1, 1))

    added: list[str] = []
    monkeypatch.setattr(
        proactivity, "add_suggestion", lambda text, code: added.append(text) or 1
    )
    events: list[Event] = []
    monkeypatch.setattr(proactivity, "publish", lambda e: events.append(e))

    proactivity._handle_trigger(Event("proactivity.trigger", {"name": "context_hint"}))
    assert added == []
    assert events == []


def test_event_greeting_allowed(monkeypatch, tmp_path):
    """Когда событие близко, подсказка проходит."""

    monkeypatch.setattr(memory_db, "DB_PATH", tmp_path / "memory.sqlite3")
    monkeypatch.setenv("JARVIS_DB_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(
        proactivity,
        "load_playbook",
        lambda: {"context_hint": {"prompt": "С днём рождения!"}},
    )
    monkeypatch.setattr(user_events, "load_event_date", lambda k: dt.date(1988, 5, 7))
    monkeypatch.setattr(user_events, "_today", lambda: dt.date(2024, 5, 5))

    added: list[str] = []
    monkeypatch.setattr(proactivity, "add_suggestion", lambda text, code: added.append(text) or 1)
    events: list[Event] = []
    monkeypatch.setattr(proactivity, "publish", lambda e: events.append(e))

    proactivity._handle_trigger(Event("proactivity.trigger", {"name": "context_hint"}))
    assert added
    assert events and events[0].attrs["text"] == "С днём рождения!"


def test_add_suggestion_dedup(monkeypatch, tmp_path):
    """Повторяющиеся по ``reason_code`` подсказки не добавляются повторно."""

    monkeypatch.setattr(memory_db, "DB_PATH", tmp_path / "memory.sqlite3")
    monkeypatch.setenv("JARVIS_DB_KEY", Fernet.generate_key().decode())

    first = writer.add_suggestion("С днём рождения, Алексей!", "ctx")
    assert first is not None
    second = writer.add_suggestion("Поздравляю с днем рождения, Алексей", "ctx")
    assert second is None


def test_trigger_dedup_by_code(monkeypatch, tmp_path):
    """Два триггера с одинаковым ``code`` создают только одну подсказку."""

    monkeypatch.setattr(memory_db, "DB_PATH", tmp_path / "memory.sqlite3")
    monkeypatch.setenv("JARVIS_DB_KEY", Fernet.generate_key().decode())

    monkeypatch.setattr(
        proactivity,
        "load_playbook",
        lambda: {"context_hint": {"prompt": "Первый", "code": "ctx"}},
    )
    events: list[Event] = []
    monkeypatch.setattr(proactivity, "publish", lambda e: events.append(e))

    proactivity._handle_trigger(Event("proactivity.trigger", {"name": "context_hint"}))
    proactivity._handle_trigger(Event("proactivity.trigger", {"name": "context_hint"}))

    with memory_db.get_connection() as conn:
        count = conn.execute("SELECT COUNT(*) FROM suggestions").fetchone()[0]

    assert count == 1
    assert len(events) == 1
