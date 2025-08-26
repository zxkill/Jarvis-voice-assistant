import threading
from core import events as core_events
import emotion.sounds as sounds


class DummySD:
    """Заглушка звукового устройства для тестов."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def play(self, data, rate, blocking: bool = False) -> None:  # pragma: no cover - данные не используются
        self.calls.append("play")


def test_play_idle_respects_presence(monkeypatch):
    """Драйвер не должен воспроизводить дыхание при наличии пользователя."""

    dummy_sd = DummySD()
    monkeypatch.setattr(sounds, "sd", dummy_sd)
    # Отключаем запуск фонового потока, чтобы не создавать гонок в тесте.
    monkeypatch.setattr(threading.Thread, "start", lambda self: None)
    # Создаём драйвер и подменяем воспроизведение эффекта на запись вызова.
    driver = sounds.EmotionSoundDriver()
    monkeypatch.setattr(sounds, "_idle_breath_last", -sounds.MIN_IDLE_BREATH_COOLDOWN)
    driver._effects = {
        "IDLE_BREATH": sounds._Effect(
            files=["breath.wav"], gain=0.0, cooldown=0.0,
            last_played=-sounds.MIN_IDLE_BREATH_COOLDOWN,
        )
    }
    monkeypatch.setattr(driver, "_play_effect", lambda name: dummy_sd.calls.append(name))

    # Без пользователя в кадре дыхание должно воспроизводиться.
    driver.play_idle_effect()
    assert dummy_sd.calls == ["IDLE_BREATH"]

    # Отмечаем присутствие лица и проверяем, что дыхание не звучит.
    driver._on_presence_update(core_events.Event(kind="presence.update", attrs={"present": True}))
    driver.play_idle_effect()
    assert dummy_sd.calls == ["IDLE_BREATH"]
