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
    monkeypatch.setattr(sounds, "_load_manifest", lambda: effects)
    sounds.play_effect("wake")
    assert dummy_sd.calls == ["play"]

