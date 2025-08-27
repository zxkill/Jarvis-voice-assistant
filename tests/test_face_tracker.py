import types

from sensors.vision import face_tracker


def test_smoothing_and_events(monkeypatch):
    """Проверяем экспоненциальное сглаживание и публикацию событий."""

    sent = []
    monkeypatch.setattr(face_tracker, "_send_track", lambda dx, dy, dt: sent.append((dx, dy, dt)))
    events = []
    monkeypatch.setattr(face_tracker, "publish", lambda e: events.append(e))

    tr = face_tracker.FaceTracker(alpha=0.5)
    tr.update(True, 10.0, 0.0, 40)
    assert sent[-1][0] == 5.0
    assert events[-1].attrs["dx"] == 5.0

    tr.update(True, 20.0, 0.0, 40)
    assert round(sent[-1][0], 1) == 12.5
    assert round(events[-1].attrs["dx"], 1) == 12.5

    tr.update(False, 0.0, 0.0, 40)
    assert events[-1].attrs["detected"] is False
