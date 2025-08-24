import emotion.sounds as sounds
from emotion.state import Emotion
from core import events as core_events


class DummySD:
    def __init__(self):
        self.calls = []

    def stop(self):
        self.calls.append("stop")

    def play(self, data, rate, blocking=False):
        self.calls.append("play")


def test_sound_stops_on_emotion_change(monkeypatch):
    core_events._subscribers.clear()
    core_events._global_subscribers.clear()

    dummy_sd = DummySD()
    monkeypatch.setattr(sounds, "sd", dummy_sd)
    monkeypatch.setattr(sounds, "_read_wav", lambda path: (0, 44100))

    effects = {
        "SLEEPY": sounds._Effect(files=["sleep.wav"], gain=0.0, cooldown=0.0),
        "HAPPY": sounds._Effect(files=["happy.wav"], gain=0.0, cooldown=0.0),
    }
    monkeypatch.setattr(sounds, "_load_manifest", lambda: effects)

    sounds.EmotionSoundDriver()

    core_events.publish(core_events.Event(kind="emotion_changed", attrs={"emotion": Emotion.SLEEPY}))
    core_events.publish(core_events.Event(kind="emotion_changed", attrs={"emotion": Emotion.HAPPY}))

    assert dummy_sd.calls == ["stop", "play", "stop", "play"]

