import core.events as events
from sensors.vision.presence import PresenceDetector


def test_presence_triggers_face_tracker():
    """presence.update и vision.face_tracker публикуются согласованно."""
    events._subscribers.clear()
    events._global_subscribers.clear()
    presence_events = []
    tracker_events = []
    events.subscribe("presence.update", presence_events.append)
    events.subscribe("vision.face_tracker", tracker_events.append)

    det = PresenceDetector(
        camera_index=0,
        frame_interval_ms=100,
        absent_after_sec=0.0,
        alpha=1.0,
        present_th=0.5,
        absent_th=0.5,
        show_window=False,
    )

    det.process_detection(True, 0.2, 0.3)
    det.process_detection(False)

    assert presence_events[0].attrs["present"] is True
    assert tracker_events[0].attrs["present"] is True
    assert tracker_events[0].attrs["x"] == 0.2
    assert presence_events[1].attrs["present"] is False
    assert tracker_events[1].attrs == {"present": False}
