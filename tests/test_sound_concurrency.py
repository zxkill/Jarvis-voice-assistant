import time
import concurrent.futures
import emotion.sounds as sounds


class SlowSD:
    """Простая заглушка `sounddevice` с задержкой.

    Задержка имитирует длительное воспроизведение, чтобы несколько потоков
    одновременно входили в критическую секцию и провоцировали гонку.
    """

    def __init__(self) -> None:
        self.calls = []

    def play(self, data, rate, blocking: bool = False) -> None:  # pragma: no cover - простая заглушка
        self.calls.append("play")
        time.sleep(0.01)


def test_play_effect_thread_safe(monkeypatch):
    """Одновременные вызовы не должны приводить к повторному звуку."""

    dummy_sd = SlowSD()
    monkeypatch.setattr(sounds, "sd", dummy_sd)
    # Возвращаем простые данные, пригодные для умножения на громкость
    monkeypatch.setattr(sounds, "_read_wav", lambda path: (1.0, 44100))
    effect = sounds._Effect(files=["sigh.wav"], gain=0.0, cooldown=10.0)
    # Подменяем глобальный кеш эффектов тестовым экземпляром
    monkeypatch.setattr(sounds, "_EFFECTS", {"SIGH": effect})

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
        list(pool.map(lambda _: sounds.play_effect("sigh"), range(5)))

    # Без блокировки звук сыграл бы пять раз; после исправления — ровно один
    assert dummy_sd.calls == ["play"]
