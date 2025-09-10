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
    """Сохранение и восстановление настроения между перезапусками."""

    monkeypatch.setitem(
        sys.modules,
        "working_tts",
        types.SimpleNamespace(speak_async=lambda *a, **k: None),
    )
    import emotion.manager as mgr_module
    monkeypatch.setattr(mgr_module, "is_day_off", lambda day=None: (False, None))

    # Избавляемся от сглаживания настроения для предсказуемых результатов
    import emotion.mood as mood

    def fake_load_config(self):
        self._valence_factor = 1.0
        self._arousal_factor = 1.0
        self._ema_alpha = 1.0

    monkeypatch.setattr(mood.Mood, "_load_config", fake_load_config)

    from emotion.manager import EmotionManager

    db_file = tmp_path / "memory.sqlite3"
    monkeypatch.setattr(db, "DB_PATH", db_file)

    mgr = EmotionManager()
    core_events.subscribe("emotion_changed", lambda e: None)
    mgr.start()

    # Эмулируем серию успешных запросов → настроение уходит в максимум
    for _ in range(3):
        core_events.publish(Event(kind="dialog.success"))
    assert mgr._state.mood.as_tuple() == (1.0, 1.0)

    # Новый экземпляр должен восстановить настроение из БД
    core_events._subscribers.clear()
    core_events._global_subscribers.clear()
    mgr2 = EmotionManager()
    assert mgr2._state.mood.as_tuple() == (1.0, 1.0)

    # Теперь имитируем ошибки → валентность становится отрицательной
    core_events.subscribe("emotion_changed", lambda e: None)
    for _ in range(2):
        core_events.publish(Event(kind="dialog.failure"))
    assert mgr2._state.mood.as_tuple() == (-1.0, 1.0)

    # После очередного "перезапуска" значение должно сохраниться
    core_events._subscribers.clear()
    core_events._global_subscribers.clear()
    mgr3 = EmotionManager()
    assert mgr3._state.mood.as_tuple() == (-1.0, 1.0)

    # Останавливаем менеджеры, чтобы фоновые таймеры не мешали другим тестам
    mgr.stop()
    mgr2.stop()
    mgr3.stop()
