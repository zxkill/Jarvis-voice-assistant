"""Тесты коротких "дыханий" драйвера звуковых эмоций."""

import emotion.sounds as sounds


class DummySD:
    """Простой мок ``sounddevice`` для проверки вызовов ``play``."""

    def __init__(self) -> None:  # type: ignore[no-untyped-def]
        self.calls: list[tuple[str, int]] = []

    def play(self, data, rate, blocking: bool = False) -> None:  # type: ignore[no-untyped-def]
        self.calls.append((data, rate))


def test_idle_effect_with_cooldown(monkeypatch, capsys):  # type: ignore[no-untyped-def]
    """Проверяем, что повторный запуск игнорируется из-за cooldown."""

    dummy_sd = DummySD()
    monkeypatch.setattr(sounds, "sd", dummy_sd)
    # возвращаем имя файла как данные, чтобы удобно проверить выбор
    selected: list[str] = []

    def fake_read(path: str):  # type: ignore[no-untyped-def]
        """Сохраняем путь для проверки и возвращаем заглушку аудио."""
        selected.append(path)
        return 0.0, 44100

    monkeypatch.setattr(sounds, "_read_wav", fake_read)

    driver = sounds.EmotionSoundDriver()
    driver._effects = {
        "IDLE_BREATH": sounds._Effect(files=["breath.wav"], gain=0.0, cooldown=1.0)
    }
    driver.log.setLevel("DEBUG")

    # фиксируем выбор файла
    monkeypatch.setattr(sounds.random, "choice", lambda seq: seq[0])

    now = [100.0]
    monkeypatch.setattr(sounds.time, "monotonic", lambda: now[0])

    driver.play_idle_effect()
    driver.play_idle_effect()  # вторая попытка должна быть пропущена

    assert dummy_sd.calls == [(0.0, 44100)]
    assert selected == ["breath.wav"]
    # проверяем наличие сообщения о пропуске из-за cooldown
    captured = capsys.readouterr()
    assert "skip IDLE_BREATH" in captured.err

    now[0] += 1.5  # превышаем cooldown
    driver.play_idle_effect()
    assert len(dummy_sd.calls) == 2


def test_idle_effect_repeat(monkeypatch):  # type: ignore[no-untyped-def]
    """Повторный запуск по параметру repeat воспроизводит звук несколько раз."""

    dummy_sd = DummySD()
    monkeypatch.setattr(sounds, "sd", dummy_sd)
    monkeypatch.setattr(sounds, "_read_wav", lambda path: (0.0, 44100))
    driver = sounds.EmotionSoundDriver()
    driver._effects = {
        "IDLE_BREATH": sounds._Effect(files=["breath.wav"], gain=0.0, cooldown=0.0, repeat=2)
    }

    driver.play_idle_effect()

    assert dummy_sd.calls == [(0.0, 44100), (0.0, 44100)]

