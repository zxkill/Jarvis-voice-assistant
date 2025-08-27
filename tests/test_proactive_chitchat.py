import sys
import time
import types
import threading

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
    # --- Пользователь ушёл: уведомление приходит в Telegram ---------
    events.clear()
    sent.clear()
    engine.present = False
    db.set_last_smalltalk_ts(0)
    engine._last_command_ts = time.time() - engine.idle_threshold_sec - 0.1
    time.sleep(1.5)
    assert len(events) == 1
    assert events[0].attrs["present"] is False
    assert sent and all(ch == "telegram" for ch, _ in sent)


def test_dialog_events_on_response(monkeypatch):
    """Убеждаемся, что ответы пользователя публикуют события ``dialog.*`` с trace_id."""
    events: list[tuple[str, Event]] = []
    core_events._subscribers.clear()

    # Отключаем фоновый поток ``_idle_loop``, чтобы тест не зависал.
    real_thread = threading.Thread

    def fake_thread(*args, **kwargs):
        target = kwargs.get("target")
        if target is not None:
            return types.SimpleNamespace(start=lambda: None)
        return real_thread(*args, **kwargs)

    monkeypatch.setattr(threading, "Thread", fake_thread)

    engine = ProactiveEngine(
        Policy(PolicyConfig()),
        idle_threshold_sec=10**6,
        smalltalk_interval_sec=10**6,
        check_period_sec=10**6,
    )
    # Возвращаем оригинальный класс ``Thread`` после создания движка.
    monkeypatch.setattr(threading, "Thread", real_thread)
    monkeypatch.setattr(engine, "_send", lambda *a, **k: True)

    core_events.subscribe("dialog.success", lambda e: events.append(("success", e)))
    core_events.subscribe("dialog.failure", lambda e: events.append(("failure", e)))

    trace_id = "trace-dialog"
    core_events.publish(
        Event(
            kind="suggestion.created",
            attrs={"text": "выпей воды", "suggestion_id": 1, "trace_id": trace_id},
        )
    )
    core_events.publish(Event(kind="telegram.message", attrs={"text": "да"}))
    time.sleep(0.1)
    assert events and events[0][0] == "success"
    assert events[0][1].attrs["trace_id"] == trace_id

    events.clear()
    core_events.publish(
        Event(
            kind="suggestion.created",
            attrs={"text": "отдохни", "suggestion_id": 2, "trace_id": trace_id},
        )
    )
    core_events.publish(Event(kind="telegram.message", attrs={"text": "нет"}))
    time.sleep(0.1)
    assert events and events[0][0] == "failure"
    assert events[0][1].attrs["trace_id"] == trace_id
