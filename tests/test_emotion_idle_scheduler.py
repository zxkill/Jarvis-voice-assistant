import time
import random

import core.events as core_events

from emotion import manager as emotion_manager
from emotion.manager import EmotionManager
from emotion.state import EmotionState, Emotion


def setup_function(function):
    # Очистка подписчиков перед каждым тестом
    core_events._subscribers.clear()
    core_events._global_subscribers.clear()


def test_idle_timer_switches_emotion_and_logs(monkeypatch):
    """Проверяем, что таймер простоя вызывает смену эмоции и логирование."""
    monkeypatch.setattr(emotion_manager, "IDLE_TIMEOUT_SEC", 0.05)

    def fake_next_idle(self):
        self.current = Emotion.HAPPY
        return Emotion.HAPPY

    monkeypatch.setattr(emotion_manager.EmotionState, "get_next_idle", fake_next_idle)

    messages: list[str] = []
    monkeypatch.setattr(emotion_manager.log, "info", lambda msg, *a, **k: messages.append(msg % a))

    mgr = EmotionManager()
    events: list[Emotion] = []
    core_events.subscribe("emotion_changed", lambda e: events.append(e.attrs["emotion"]))

    mgr.start()
    monkeypatch.setattr(mgr, "_reset_idle_timer", lambda: None)
    time.sleep(emotion_manager.IDLE_TIMEOUT_SEC + 0.02)
    mgr.stop()

    assert events == [Emotion.NEUTRAL, Emotion.HAPPY]
    assert any("idle timer" in m for m in messages)


def test_time_based_and_micro_emotions(monkeypatch):
    """Тестируем выбор эмоций по времени суток и микро-эмоции."""
    state = EmotionState()

    # Утро
    monkeypatch.setattr(time, "localtime", lambda: type("t", (), {"tm_hour": 7})())
    assert state.get_time_based_emotion() == Emotion.SLEEPY

    # День
    monkeypatch.setattr(time, "localtime", lambda: type("t", (), {"tm_hour": 13})())
    assert state.get_time_based_emotion() == Emotion.HAPPY

    # Поздний вечер
    monkeypatch.setattr(time, "localtime", lambda: type("t", (), {"tm_hour": 23})())
    assert state.get_time_based_emotion() == Emotion.TIRED

    # Микро-эмоция при совпадении базовой с текущей
    state.current = Emotion.SLEEPY
    monkeypatch.setattr(time, "localtime", lambda: type("t", (), {"tm_hour": 7})())
    monkeypatch.setattr(random, "choice", lambda seq: seq[0])
    assert state.get_next_idle() == Emotion.SQUINT
