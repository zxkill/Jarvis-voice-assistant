import sys
import types

from core.events import subscribe, _subscribers


def test_mood_change_triggers_emotion(monkeypatch):
    """Любое сохранение настроения публикует новую эмоцию."""
    # Избегаем зависимостей от аудио‑библиотек
    monkeypatch.setitem(sys.modules, "sounddevice", types.SimpleNamespace())
    monkeypatch.setitem(
        sys.modules,
        "working_tts",
        types.SimpleNamespace(speak_async=lambda *a, **k: None),
    )
    from emotion.manager import EmotionManager
    from emotion.state import Emotion

    manager = EmotionManager()
    events: list[Emotion] = []

    def handler(event):
        events.append(event.attrs["emotion"])

    subscribe("emotion_changed", handler)

    # Имитация внешнего изменения настроения: обновляем координаты
    manager._state.mood.update(1.0, 1.0)
    manager._state.mood.save()

    assert events and events[-1] is Emotion.HAPPY

    # Чистим подписчиков менеджера и тестовой функции, чтобы не влиять на другие тесты
    for lst in _subscribers.values():
        for cb in list(lst):
            if cb is handler or getattr(cb, "__self__", None) is manager:
                lst.remove(cb)
