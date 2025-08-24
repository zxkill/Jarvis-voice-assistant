import types

from emotion.manager import EmotionManager
from emotion.state import Emotion
from core import events as core_events


def _setup(monkeypatch):
    core_events._subscribers.clear()
    core_events._global_subscribers.clear()
    callbacks = []

    class FakeTimer:
        def __init__(self, delay, func):
            callbacks.append(func)
        def start(self):
            pass

    fake_time = types.SimpleNamespace(now=0.0)
    monkeypatch.setattr('emotion.manager.Timer', FakeTimer)
    monkeypatch.setattr(
        'emotion.manager.time',
        types.SimpleNamespace(monotonic=lambda: fake_time.now, sleep=lambda x: None),
    )
    mgr = EmotionManager()
    mgr.start()
    return mgr, callbacks, fake_time


def test_voice_wakeup_thinking(monkeypatch):
    mgr, callbacks, fake_time = _setup(monkeypatch)
    core_events.publish(core_events.Event('presence.update', {'present': False}))
    core_events.publish(core_events.Event('speech.recognized', {'text': 'cmd'}))
    assert mgr._state.current == Emotion.SURPRISED

    fake_time.now = 1.0
    core_events.publish(core_events.Event('user_query_started', {}))
    assert mgr._state.current == Emotion.SURPRISED

    fake_time.now = 3.0
    callbacks.pop()()
    assert mgr._state.current == Emotion.THINKING


def test_voice_wakeup_neutral_when_done(monkeypatch):
    mgr, callbacks, fake_time = _setup(monkeypatch)
    core_events.publish(core_events.Event('presence.update', {'present': False}))
    core_events.publish(core_events.Event('speech.recognized', {'text': 'cmd'}))
    assert mgr._state.current == Emotion.SURPRISED

    fake_time.now = 0.5
    core_events.publish(core_events.Event('user_query_started', {}))
    fake_time.now = 1.0
    core_events.publish(core_events.Event('user_query_ended', {}))
    assert mgr._state.current == Emotion.SURPRISED

    fake_time.now = 3.0
    callbacks.pop()()
    assert mgr._state.current == Emotion.NEUTRAL
