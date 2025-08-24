import core.events as core_events
from core.events import Event
from emotion.manager import EmotionManager
from emotion.state import Emotion


def test_emotion_queue_during_speech():
    core_events._subscribers.clear()
    core_events._global_subscribers.clear()

    mgr = EmotionManager()
    events = []
    core_events.subscribe("emotion_changed", lambda e: events.append(e.attrs["emotion"]))

    mgr.start()
    assert events == [Emotion.NEUTRAL]

    core_events.publish(Event(kind="speech.synthesis_started"))
    mgr._state.set(Emotion.HAPPY)
    mgr._publish_emotion(Emotion.HAPPY)
    assert events == [Emotion.NEUTRAL]

    mgr._state.set(Emotion.SAD)
    mgr._publish_emotion(Emotion.SAD)
    assert events == [Emotion.NEUTRAL]

    core_events.publish(Event(kind="speech.synthesis_finished"))
    assert events == [Emotion.NEUTRAL, Emotion.SAD]
