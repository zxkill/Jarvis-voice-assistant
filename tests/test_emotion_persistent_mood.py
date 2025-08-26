import core.events as core_events
from core.events import Event
import memory.db as db
from emotion.manager import EmotionManager
from emotion.state import Emotion


def setup_function(function):
    # Очищаем всех подписчиков event bus перед каждым тестом
    core_events._subscribers.clear()
    core_events._global_subscribers.clear()


def test_emotion_persistent_mood(monkeypatch, tmp_path):
    """Сохранение и восстановление уровня настроения между перезапусками."""
    db_file = tmp_path / "memory.sqlite3"
    monkeypatch.setattr(db, "DB_PATH", db_file)

    mgr = EmotionManager()
    events: list[Emotion] = []
    core_events.subscribe("emotion_changed", lambda e: events.append(e.attrs["emotion"]))
    mgr.start()

    # Эмулируем серию успешных запросов
    for _ in range(3):
        core_events.publish(Event(kind="dialog.success"))
    assert mgr._state.mood == 30
    assert events[-1] == Emotion.HAPPY

    # Новый экземпляр должен восстановить настроение из БД
    core_events._subscribers.clear()
    core_events._global_subscribers.clear()
    mgr2 = EmotionManager()
    assert mgr2._state.mood == 30

    # Теперь имитируем ошибки
    core_events.subscribe("emotion_changed", lambda e: events.append(e.attrs["emotion"]))
    for _ in range(2):
        core_events.publish(Event(kind="dialog.failure"))
    assert mgr2._state.mood == 10
    assert events[-1] == Emotion.FRUSTRATED

    # После очередного "перезапуска" значение должно сохраниться
    core_events._subscribers.clear()
    core_events._global_subscribers.clear()
    mgr3 = EmotionManager()
    assert mgr3._state.mood == 10

    # Останавливаем менеджеры, чтобы фоновые таймеры не мешали другим тестам
    mgr.stop()
    mgr2.stop()
    mgr3.stop()
