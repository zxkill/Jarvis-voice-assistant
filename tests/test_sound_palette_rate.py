import types
import emotion.sounds as sounds
from utils.rate_limiter import RateLimiter


class DummySD:
    """Простая заглушка звукового устройства."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def play(self, data, rate, blocking: bool = False) -> None:  # pragma: no cover - запись вызовов
        self.calls.append("play")


def test_palette_selection(monkeypatch):
    """Выбор эффекта должен учитывать текущую палитру."""

    dummy_sd = DummySD()
    monkeypatch.setattr(sounds, "sd", dummy_sd)
    monkeypatch.setattr(sounds, "is_quiet_now", lambda: False)
    monkeypatch.setattr(sounds, "random", types.SimpleNamespace(choice=lambda seq: seq[0]))
    paths: list[str] = []

    def fake_read(path: str):  # pragma: no cover - фиксация пути
        paths.append(path)
        return 0, 44100

    monkeypatch.setattr(sounds, "_read_wav", fake_read)
    effect_bright = sounds._Effect(files=["bright.wav"], gain=0.0, cooldown=0.0)
    effect_default = sounds._Effect(files=["default.wav"], gain=0.0, cooldown=0.0)
    monkeypatch.setattr(
        sounds,
        "_EFFECTS",
        {"BRIGHT:ACK": effect_bright, "ACK": effect_default},
    )
    monkeypatch.setattr(sounds, "_GLOBAL_LIMITER", None)
    monkeypatch.setattr(sounds, "_CURRENT_PALETTE", "BRIGHT")

    sounds.play_effect("ack")

    assert dummy_sd.calls == ["play"]
    assert paths == ["bright.wav"]


def test_global_rate_limit(monkeypatch):
    """Глобальный лимитер блокирует слишком частые эффекты."""

    dummy_sd = DummySD()
    monkeypatch.setattr(sounds, "sd", dummy_sd)
    monkeypatch.setattr(sounds, "is_quiet_now", lambda: False)
    monkeypatch.setattr(sounds, "_read_wav", lambda path: (0, 44100))
    effect_a = sounds._Effect(files=["a.wav"], gain=0.0, cooldown=0.0)
    effect_b = sounds._Effect(files=["b.wav"], gain=0.0, cooldown=0.0)
    monkeypatch.setattr(sounds, "_EFFECTS", {"A": effect_a, "B": effect_b})
    monkeypatch.setattr(sounds, "_CURRENT_PALETTE", "")
    times = [0.0]

    def fake_time() -> float:
        return times[0]

    monkeypatch.setattr(sounds.time, "monotonic", fake_time)
    rl = RateLimiter(1, 1.0, time_func=fake_time)
    monkeypatch.setattr(sounds, "_GLOBAL_LIMITER", rl)

    sounds.play_effect("A")
    sounds.play_effect("B")  # заблокирован глобальным лимитом
    times[0] = 1.0
    sounds.play_effect("B")

    assert dummy_sd.calls == ["play", "play"]
