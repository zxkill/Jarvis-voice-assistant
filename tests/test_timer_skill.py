import time
import types
import sys

# Заглушка sounddevice, чтобы тесты не требовали PortAudio
sd_stub = types.SimpleNamespace(play=lambda *a, **k: None, wait=lambda: None, stop=lambda: None)
sys.modules.setdefault("sounddevice", sd_stub)

# Заглушка отправки в Telegram, чтобы не выполнять сетевые запросы
tg_stub = types.SimpleNamespace(send=lambda *a, **k: None)
sys.modules.setdefault("notifiers.telegram", tg_stub)

# Заглушка TTS, чтобы избежать зависимости от PortAudio и моделей
sys.modules.setdefault("working_tts", types.SimpleNamespace(stop_speaking=lambda: None))

from memory.db import get_connection
from skills import timer_alarm as ta
from skills import stop as stop_skill


def setup_module(module):
    # очистим состояние между тестами
    ta._TIMERS.clear()
    ta._ALERTS.clear()
    with get_connection() as conn:
        conn.execute("DELETE FROM timers")
        conn.execute("DELETE FROM presence_sessions")


def teardown_module(module):
    # очищаем импортированные модули, чтобы не мешать другим тестам
    sys.modules.pop("notifiers", None)
    sys.modules.pop("notifiers.telegram", None)
    sys.modules.pop("working_tts", None)


def test_user_present_detection():
    assert not ta._user_present()
    now = int(time.time())
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO presence_sessions(user_id, start_ts) VALUES('u', ?)",
            (now,),
        )
    assert ta._user_present()
    with get_connection() as conn:
        conn.execute("UPDATE presence_sessions SET end_ts=?", (now,))
    assert not ta._user_present()


def test_fire_sends_telegram_when_absent(monkeypatch):
    calls = []
    monkeypatch.setattr(ta, "_user_present", lambda: False)
    monkeypatch.setattr(ta, "_tg_send", lambda msg: calls.append(msg))
    monkeypatch.setattr(ta, "_beep", lambda *a, **k: None)
    monkeypatch.setattr(ta, "_speak", lambda *a, **k: None)
    monkeypatch.setattr(ta, "get_driver", lambda: types.SimpleNamespace(draw=lambda *a, **k: None))
    with get_connection() as conn:
        conn.execute("INSERT OR REPLACE INTO timers(label, typ, end_ts) VALUES('t', 'timer', 0)")
    ta._fire("t", "timer")
    assert calls  # сообщение отправлено
    with get_connection() as conn:
        assert conn.execute("SELECT 1 FROM timers WHERE label='t'").fetchone() is None
    assert "t" not in ta._ALERTS


def test_fire_keeps_timer_until_stop(monkeypatch):
    monkeypatch.setattr(ta, "_user_present", lambda: True)
    monkeypatch.setattr(ta, "_beep", lambda *a, **k: None)
    monkeypatch.setattr(ta, "_speak", lambda *a, **k: None)
    monkeypatch.setattr(ta, "get_driver", lambda: types.SimpleNamespace(draw=lambda *a, **k: None))
    monkeypatch.setattr(ta, "_alert_loop", lambda ev: None)
    with get_connection() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO timers(label, typ, end_ts) VALUES('a', 'timer', ?)",
            (int(time.time()),),
        )
    ta._fire("a", "timer")
    assert "a" in ta._ALERTS
    with get_connection() as conn:
        assert conn.execute("SELECT 1 FROM timers WHERE label='a'").fetchone() is not None
    ta._stop("a")
    assert "a" not in ta._ALERTS
    with get_connection() as conn:
        assert conn.execute("SELECT 1 FROM timers WHERE label='a'").fetchone() is None


def test_global_stop_stops_timer(monkeypatch):
    monkeypatch.setattr(ta, "_user_present", lambda: True)
    monkeypatch.setattr(ta, "_beep", lambda *a, **k: None)
    monkeypatch.setattr(ta, "_speak", lambda *a, **k: None)
    monkeypatch.setattr(ta, "get_driver", lambda: types.SimpleNamespace(draw=lambda *a, **k: None))
    monkeypatch.setattr(ta, "_alert_loop", lambda ev: None)
    with get_connection() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO timers(label, typ, end_ts) VALUES('b', 'timer', ?)",
            (int(time.time()),),
        )
    ta._fire("b", "timer")
    assert "b" in ta._ALERTS
    resp = stop_skill.handle("стоп")
    assert resp == ""
    assert "b" not in ta._ALERTS
    with get_connection() as conn:
        assert conn.execute("SELECT 1 FROM timers WHERE label='b'").fetchone() is None
