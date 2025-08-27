import core.events as core_events
from core.events import Event
from emotion.manager import EmotionManager
from emotion.state import Emotion
import emotion.mood as mood
import memory.db as db
import pytest
from emotion import policy


@pytest.fixture(autouse=True)
def clean_bus():
    """Очистка подписчиков перед каждым тестом."""
    core_events._subscribers.clear()
    core_events._global_subscribers.clear()
    policy._last_icon = None
    policy._last_switch_ts = 0.0


@pytest.fixture
def manager(monkeypatch, tmp_path):
    """Создаёт менеджер эмоций с изолированной БД и конфигурацией."""
    # Перенаправляем путь к БД во временную директорию, чтобы тесты не влияли друг на друга
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "memory.sqlite3")

    # Настраиваем Mood так, чтобы изменения применялись без сглаживания
    def fake_load_config(self):
        self._valence_factor = 1.0
        self._arousal_factor = 1.0
        self._ema_alpha = 1.0
    monkeypatch.setattr(mood.Mood, "_load_config", fake_load_config)

    # Отключаем реальные таймеры, чтобы тесты выполнялись синхронно
    class DummyTimer:
        def __init__(self, *a, **kw):
            pass
        def start(self):
            pass
        def cancel(self):
            pass
    monkeypatch.setattr("emotion.manager.Timer", DummyTimer)

    mgr = EmotionManager()
    events: list[Emotion] = []
    core_events.subscribe("emotion_changed", lambda e: events.append(e.attrs["emotion"]))
    mgr.start()
    yield mgr, events
    mgr.stop()


def test_dialog_success(manager):
    mgr, events = manager
    core_events.publish(Event(kind="dialog.success"))
    assert events[-1] == Emotion.HAPPY


def test_dialog_failure(manager):
    mgr, events = manager
    core_events.publish(Event(kind="dialog.failure"))
    assert events[-1] == Emotion.ANGRY


def test_presence_absence(manager):
    mgr, events = manager
    core_events.publish(Event(kind="presence.update", attrs={"present": False}))
    assert events[-1] == Emotion.SAD


def test_weather_rain(manager):
    mgr, events = manager
    core_events.publish(Event(kind="weather.update", attrs={"condition": "rain"}))
    assert events[-1] == Emotion.SAD

