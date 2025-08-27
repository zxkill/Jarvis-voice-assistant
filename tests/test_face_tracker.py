import pytest

from sensors.vision.face_tracker import FaceTracker


class DummyPublish:
    def __init__(self):
        self.events = []

    def __call__(self, event):
        self.events.append(event)


def test_face_tracker_smoothing_and_events(monkeypatch):
    """Трекер сглаживает координаты и публикует события о статусе лица."""

    dummy_publish = DummyPublish()
    monkeypatch.setattr("sensors.vision.face_tracker.publish", dummy_publish)

    calls = []
    monkeypatch.setattr(
        "sensors.vision.face_tracker._update_track",
        lambda detected, dx, dy, dt: calls.append((detected, dx, dy, dt)),
    )

    tr = FaceTracker(alpha=0.5)

    # При первом обнаружении координаты сглаживаются к половине.
    tr.update(True, 10.0, -6.0, 40)
    assert calls[-1] == (True, 5.0, -3.0, 40)
    assert dummy_publish.events[-1].attrs["detected"] is True

    # При потере лица сервоприводы останавливаются и координаты обнуляются.
    tr.update(False, 0.0, 0.0, 40)
    assert calls[-1][0] is False
    assert tr.dx == 0.0 and tr.dy == 0.0
    assert dummy_publish.events[-1].attrs["detected"] is False
