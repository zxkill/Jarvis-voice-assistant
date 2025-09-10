
import types
import sys

from core import events as core_events
from core.events import Event
from proactive.policy import Policy, PolicyConfig
from proactive.engine import ProactiveEngine
from analysis import proactivity
import memory.db as db


def test_trigger_sends_telegram_when_absent(monkeypatch, tmp_path):
    """LLM-подсказка уходит в Telegram, если пользователя нет рядом."""

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
    monkeypatch.setattr("proactive.policy.is_quiet_now", lambda: False)

    core_events._subscribers.clear()
    core_events.subscribe("proactivity.trigger", proactivity._handle_trigger)
    policy = Policy(PolicyConfig())
    ProactiveEngine(policy)

    # Пользователь отсутствует
    core_events.publish(Event(kind="presence.update", attrs={"present": False}))

    # Искусственно возвращаем идентификатор подсказки, не обращаясь к БД
    monkeypatch.setattr(proactivity, "add_suggestion", lambda text, code: 1)

    # Запускаем сценарий плейбука и ждём завершения обработки
    thread = core_events.fire_proactive_trigger("event", "health_check")
    thread.join(timeout=2)

    assert sent == [
        (
            "telegram",
            "Вежливо напомни сделать разминку, размять глаза и выпить воды.\n",
        )
    ]


def test_trigger_sends_voice_when_present(monkeypatch, tmp_path):
    """При присутствии пользователя подсказка озвучивается голосом."""

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
    monkeypatch.setattr("proactive.policy.is_quiet_now", lambda: False)

    core_events._subscribers.clear()
    core_events.subscribe("proactivity.trigger", proactivity._handle_trigger)
    policy = Policy(PolicyConfig())
    ProactiveEngine(policy)
    core_events.publish(Event(kind="presence.update", attrs={"present": True}))

    monkeypatch.setattr(proactivity, "add_suggestion", lambda text, code: 1)

    thread = core_events.fire_proactive_trigger("event", "health_check")
    thread.join(timeout=2)

    assert sent == [
        (
            "voice",
            "Вежливо напомни сделать разминку, размять глаза и выпить воды.\n",
        )
    ]

def test_emotion_reacts_to_user_query(monkeypatch):
    core_events._subscribers.clear()

    draws = []

    class DummyDriver:
        def draw(self, item):
            draws.append(item)

    # Заглушаем голосовой нотификатор до импорта EmotionManager,
    # чтобы избежать зависимости от PortAudio
    monkeypatch.setitem(
        sys.modules,
        "notifiers.voice",
        types.SimpleNamespace(send=lambda *a, **k: None),
    )

    from emotion.manager import EmotionManager
    from emotion.drivers import EmotionDisplayDriver
    from emotion.state import Emotion

    monkeypatch.setattr("emotion.drivers.get_driver", lambda: DummyDriver())

    EmotionManager()
    EmotionDisplayDriver()

    core_events.publish(Event(kind="user_query_started"))

    assert draws and draws[-1].payload == Emotion.THINKING.value
