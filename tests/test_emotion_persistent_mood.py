import sys
import types
import core.events as core_events
from core.events import Event
import memory.db as db


def setup_function(function):
    # Очищаем всех подписчиков event bus перед каждым тестом
    core_events._subscribers.clear()
    core_events._global_subscribers.clear()


def test_emotion_persistent_mood(monkeypatch, tmp_path):
    """Сохранение и восстановление уровня настроения между перезапусками."""
    monkeypatch.setitem(
        sys.modules,
        "working_tts",
        types.SimpleNamespace(speak_async=lambda *a, **k: None),
    )
    import emotion.manager as mgr_module
    monkeypatch.setattr(mgr_module, "is_day_off", lambda day=None: (False, None))
    from emotion.manager import EmotionManager
    from emotion.state import Emotion

    db_file = tmp_path / "memory.sqlite3"
    monkeypatch.setattr(db, "DB_PATH", db_file)

    mgr = EmotionManager()
    core_events.subscribe("emotion_changed", lambda e: None)
    mgr.start()

    # Эмулируем серию успешных запросов
    for _ in range(3):
        core_events.publish(Event(kind="dialog.success"))
    assert mgr._state.mood == 30

    # Новый экземпляр должен восстановить настроение из БД
    core_events._subscribers.clear()
    core_events._global_subscribers.clear()
    mgr2 = EmotionManager()
    assert mgr2._state.mood == 30

    # Теперь имитируем ошибки
    core_events.subscribe("emotion_changed", lambda e: None)
    for _ in range(2):
        core_events.publish(Event(kind="dialog.failure"))
    assert mgr2._state.mood == 10

    # После очередного "перезапуска" значение должно сохраниться
    core_events._subscribers.clear()
    core_events._global_subscribers.clear()
    mgr3 = EmotionManager()
    assert mgr3._state.mood == 10

    # Останавливаем менеджеры, чтобы фоновые таймеры не мешали другим тестам
    mgr.stop()
    mgr2.stop()
    mgr3.stop()
