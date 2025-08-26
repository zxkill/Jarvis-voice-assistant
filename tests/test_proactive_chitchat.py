import sys
import time
import types

from core import events as core_events
from core.events import Event
from proactive.engine import ProactiveEngine
from proactive.policy import Policy, PolicyConfig
import memory.db as db


def test_smalltalk_interval(monkeypatch, tmp_path):
    """Проверяем, что small-talk генерируется с нужным интервалом."""
    db_file = tmp_path / "memory.sqlite3"
    monkeypatch.setattr(db, "DB_PATH", db_file)

    sent = []

    def fake_send_tg(text):
        sent.append(("telegram", text))

    def fake_send_voice(text):
        sent.append(("voice", text))

    fake_pkg = types.SimpleNamespace(
        telegram=types.SimpleNamespace(send=fake_send_tg),
        voice=types.SimpleNamespace(send=fake_send_voice),
    )
    monkeypatch.setitem(sys.modules, "notifiers", fake_pkg)
    monkeypatch.setitem(sys.modules, "notifiers.telegram", fake_pkg.telegram)
    monkeypatch.setitem(sys.modules, "notifiers.voice", fake_pkg.voice)

    events = []
    core_events._subscribers.clear()
    engine = ProactiveEngine(
        Policy(PolicyConfig()),
        idle_threshold_sec=1,
        smalltalk_interval_sec=2,
        check_period_sec=0.5,
    )
    core_events.subscribe("suggestion.created", lambda e: events.append(e))
    core_events.publish(Event(kind="presence.update", attrs={"present": True}))

    # Ждём появления первой реплики.
    time.sleep(1.5)
    assert len(events) == 1
    assert events[0].attrs["reason_code"] == "long_silence"
    assert sent, "уведомление должно быть отправлено"

    # Интервал ещё не прошёл — новых событий нет.
    time.sleep(1)
    assert len(events) == 1

    # После ожидания интервала появляется новая реплика.
    time.sleep(2.5)
    assert len(events) >= 2
    assert len(sent) >= 2
