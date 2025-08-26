"""Тесты логирования звуковых эффектов."""

import threading
import logging

import emotion.sounds as sounds


class DummySD:
    """Заглушка ``sounddevice`` для проверки вызовов ``play``."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def play(self, data, rate, blocking: bool = False) -> None:  # pragma: no cover - данные не используются
        self.calls.append("play")


def test_play_effect_logs_caller(monkeypatch, caplog):
    """Глобальная функция логирует имя эффекта и вызывающую функцию."""

    dummy_sd = DummySD()
    monkeypatch.setattr(sounds, "sd", dummy_sd)
    monkeypatch.setattr(sounds, "is_quiet_now", lambda: False)
    monkeypatch.setattr(sounds, "_read_wav", lambda path: (0, 44100))
    monkeypatch.setattr(sounds, "_EFFECTS", {"WAKE": sounds._Effect(files=["wake.wav"], gain=0.0, cooldown=0.0)})

    def trigger() -> None:
        sounds.play_effect("wake")

    with caplog.at_level(logging.INFO):
        sounds.log.addHandler(caplog.handler)
        trigger()
        sounds.log.removeHandler(caplog.handler)

    assert dummy_sd.calls == ["play"]
    messages = [record.getMessage() for record in caplog.records]
    assert any("WAKE" in m and "trigger" in m for m in messages)


def test_driver_logs_caller(monkeypatch, caplog):
    """Драйвер также фиксирует источник вызова в логах."""

    dummy_sd = DummySD()
    monkeypatch.setattr(sounds, "sd", dummy_sd)
    monkeypatch.setattr(sounds, "is_quiet_now", lambda: False)
    monkeypatch.setattr(sounds, "_read_wav", lambda path: (0, 44100))
    monkeypatch.setattr(threading.Thread, "start", lambda self: None)

    driver = sounds.EmotionSoundDriver()
    driver._effects = {"IDLE_BREATH": sounds._Effect(files=["breath.wav"], gain=0.0, cooldown=0.0)}

    with caplog.at_level(logging.INFO):
        sounds.log.addHandler(caplog.handler)
        driver.play_idle_effect()
        sounds.log.removeHandler(caplog.handler)

    assert dummy_sd.calls == ["play"]
    messages = [record.getMessage() for record in caplog.records]
    assert any("IDLE_BREATH" in m and "play_idle_effect" in m for m in messages)

