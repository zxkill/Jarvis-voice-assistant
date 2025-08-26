"""Проверяем, что глобальный таймер предотвращает повтор дыхания."""

import emotion.sounds as sounds


class DummySD:
    """Простой мок ``sounddevice`` для проверки факта воспроизведения."""

    def __init__(self):  # type: ignore[no-untyped-def]
        self.calls: list[tuple[float, int]] = []

    def play(self, data, rate, blocking: bool = False):  # type: ignore[no-untyped-def]
        self.calls.append((data, rate))


def test_global_idle_breath_cooldown(monkeypatch):  # type: ignore[no-untyped-def]
    """Два экземпляра драйвера не воспроизводят дыхание чаще чем раз в 15 мин."""

    dummy_sd = DummySD()
    monkeypatch.setattr(sounds, "sd", dummy_sd)
    monkeypatch.setattr(sounds, "_read_wav", lambda path: (0.0, 44100))

    # Отключаем фоновый таймер, чтобы тест был детерминированным
    class SilentTimer:
        def __init__(self, *a, **k):  # type: ignore[no-untyped-def]
            pass

        def start(self) -> None:  # type: ignore[no-untyped-def]
            pass

        def cancel(self) -> None:  # type: ignore[no-untyped-def]
            pass

    monkeypatch.setattr(sounds.threading, "Timer", lambda *a, **k: SilentTimer())

    # Сбрасываем глобальную метку времени на далёкое прошлое,
    # чтобы первый вызов не был пропущен из-за глобального интервала.
    monkeypatch.setattr(
        sounds, "_idle_breath_last", -sounds.MIN_IDLE_BREATH_COOLDOWN
    )

    # Создаём два драйвера с минимальным cooldown, чтобы проверить именно
    # глобальное ограничение
    driver1 = sounds.EmotionSoundDriver()
    driver2 = sounds.EmotionSoundDriver()
    effect = sounds._Effect(
        files=["breath.wav"],
        gain=0.0,
        cooldown=0.0,
        last_played=-sounds.MIN_IDLE_BREATH_COOLDOWN,
    )
    driver1._effects = {"IDLE_BREATH": effect}
    driver2._effects = {"IDLE_BREATH": effect}

    now = [0.0]
    monkeypatch.setattr(sounds.time, "monotonic", lambda: now[0])

    driver1.play_idle_effect()
    driver2.play_idle_effect()  # должно быть пропущено из-за глобального cooldown

    assert dummy_sd.calls == [(0.0, 44100)]
