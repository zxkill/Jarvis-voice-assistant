
import datetime as dt
import types
import sys

from core import events as core_events
from core.events import Event
from proactive.policy import Policy, PolicyConfig
from proactive.engine import ProactiveEngine
from analysis import suggestions
import memory.db as db
from emotion.manager import EmotionManager
from emotion.drivers import EmotionDisplayDriver
from emotion.state import Emotion


def test_no_stretch_when_user_absent(monkeypatch, tmp_path):
    db_file = tmp_path / "memory.sqlite3"
    monkeypatch.setattr(db, "DB_PATH", db_file)

    sent = []

    def fake_send_tg(text):
        sent.append(("telegram", text))

    def fake_send_voice(text):
        sent.append(("voice", text))

    monkeypatch.setitem(
        sys.modules, "notifiers.telegram", types.SimpleNamespace(send=fake_send_tg)
    )
    monkeypatch.setitem(
        sys.modules, "notifiers.voice", types.SimpleNamespace(send=fake_send_voice)
    )

    core_events._subscribers.clear()
    # Восстанавливаем обработчик presence.update в модуле подсказок.
    core_events.subscribe("presence.update", suggestions._on_presence)
    policy = Policy(PolicyConfig())
    ProactiveEngine(policy)

    core_events.publish(Event(kind="presence.update", attrs={"present": False}))

    now = dt.datetime(2024, 1, 1, 12, 0)
    ids = suggestions.generate(now=now)

    assert not ids
    assert sent == []


def test_voice_channel_also_notifies_telegram(monkeypatch, tmp_path):
    db_file = tmp_path / "memory.sqlite3"
    monkeypatch.setattr(db, "DB_PATH", db_file)

    sent = []

    def fake_send_tg(text):
        sent.append(("telegram", text))

    def fake_send_voice(text):
        sent.append(("voice", text))

    monkeypatch.setitem(
        sys.modules, "notifiers.telegram", types.SimpleNamespace(send=fake_send_tg)
    )
    monkeypatch.setitem(
        sys.modules, "notifiers.voice", types.SimpleNamespace(send=fake_send_voice)
    )

    core_events._subscribers.clear()
    core_events.subscribe("presence.update", suggestions._on_presence)
    policy = Policy(PolicyConfig())
    ProactiveEngine(policy)
    core_events.publish(Event(kind="presence.update", attrs={"present": True}))

    now = dt.datetime(2024, 1, 1, 12, 0)
    # Пользователь не уходил более часа.
    suggestions._last_absent = now - dt.timedelta(hours=1, minutes=1)

    ids = suggestions.generate(now=now)
    suggestion_id = ids[0]

    assert ("voice", "разминка?") in sent
    assert ("telegram", "разминка?") in sent

    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT processed FROM suggestions WHERE id=?", (suggestion_id,)
        ).fetchone()
        assert row["processed"] == 1

def test_emotion_reacts_to_user_query(monkeypatch):
    core_events._subscribers.clear()

    draws = []

    class DummyDriver:
        def draw(self, item):
            draws.append(item)

    monkeypatch.setattr('emotion.drivers.get_driver', lambda: DummyDriver())

    EmotionManager()
    EmotionDisplayDriver()

    core_events.publish(Event(kind="user_query_started"))

    assert draws and draws[-1].payload == Emotion.THINKING.value
