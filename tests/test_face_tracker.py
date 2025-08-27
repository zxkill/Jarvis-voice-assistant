import core.events as events
from sensors.vision.face_tracker import FaceTracker


def test_smoothing_and_events():
    """Координаты сглаживаются EMA, события публикуются корректно."""
    events._subscribers.clear()
    events._global_subscribers.clear()
    captured = []
    events.subscribe("vision.face_tracker", captured.append)

    tracker = FaceTracker(alpha=0.5)
    tracker.update((0.0, 0.0))
    tracker.update((1.0, 1.0))
    tracker.update((0.0, 0.0))
    tracker.update(None)

    assert captured[0].attrs == {"present": True, "x": 0.0, "y": 0.0}
    assert captured[1].attrs["x"] == 0.5
    assert captured[2].attrs["x"] == 0.25
    assert captured[3].attrs == {"present": False}
