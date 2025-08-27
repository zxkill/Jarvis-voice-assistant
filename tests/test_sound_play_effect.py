import emotion.sounds as sounds


class DummySD:
    def __init__(self) -> None:
        self.calls = []

    def play(self, data, rate, blocking: bool = False) -> None:
        self.calls.append("play")


def test_play_effect(monkeypatch):
    dummy_sd = DummySD()
    monkeypatch.setattr(sounds, "sd", dummy_sd)
    monkeypatch.setattr(sounds, "_read_wav", lambda path: (0, 44100))
    effects = {
        "WAKE": sounds._Effect(files=["wake.wav"], gain=0.0, cooldown=0.0),
    }
    # Подменяем глобальный кеш эффектов, чтобы использовать тестовые данные
    monkeypatch.setattr(sounds, "_EFFECTS", effects)
    monkeypatch.setattr(sounds, "_GLOBAL_LIMITER", None)
    monkeypatch.setattr(sounds, "_CURRENT_PALETTE", "")
    sounds.play_effect("wake")
    assert dummy_sd.calls == ["play"]


def test_play_effect_respects_cooldown(monkeypatch):
    """Повторный вызов не должен воспроизводить звук чаще cooldown."""

    dummy_sd = DummySD()
    monkeypatch.setattr(sounds, "sd", dummy_sd)
    monkeypatch.setattr(sounds, "_read_wav", lambda path: (0, 44100))
    effect = sounds._Effect(files=["sigh.wav"], gain=0.0, cooldown=10.0)
    monkeypatch.setattr(sounds, "_EFFECTS", {"SIGH": effect})
    monkeypatch.setattr(sounds, "_GLOBAL_LIMITER", None)
    monkeypatch.setattr(sounds, "_CURRENT_PALETTE", "")

    sounds.play_effect("sigh")
    sounds.play_effect("sigh")  # вторая попытка до истечения cooldown

    assert dummy_sd.calls == ["play"]


def test_play_effect_repeat(monkeypatch):
    """При указании repeat звук должен проигрываться несколько раз."""

    dummy_sd = DummySD()
    monkeypatch.setattr(sounds, "sd", dummy_sd)
    monkeypatch.setattr(sounds, "_read_wav", lambda path: (0, 44100))
    effect = sounds._Effect(files=["breath.wav"], gain=0.0, cooldown=0.0, repeat=3)
    monkeypatch.setattr(sounds, "_EFFECTS", {"BREATH": effect})
    monkeypatch.setattr(sounds, "_GLOBAL_LIMITER", None)
    monkeypatch.setattr(sounds, "_CURRENT_PALETTE", "")

    sounds.play_effect("breath")

    assert dummy_sd.calls == ["play", "play", "play"]

