import sys
import types

import config
from core.events import Event


def test_mood_announcer_disabled(monkeypatch):
    """При announce_mood=False озвучивание настроения не выполняется."""
    # Подменяем модули, зависящие от внешних библиотек, чтобы избежать ошибок импорта
    monkeypatch.setitem(sys.modules, "sounddevice", types.SimpleNamespace())
    monkeypatch.setitem(
        sys.modules,
        "working_tts",
        types.SimpleNamespace(speak_async=lambda *a, **k: None),
    )
    from emotion.manager import EmotionManager

    manager = EmotionManager()
    calls: list[tuple[str, str]] = []

    def fake_announce(feeling: str, source: str) -> None:
        calls.append((feeling, source))

    monkeypatch.setattr(manager, "_announce_mood", fake_announce)
    monkeypatch.setattr(config.affect, "announce_mood", False)

    manager._on_dialog_success(Event("dialog.success"))
    manager._on_dialog_failure(Event("dialog.failure"))

    assert calls == []
