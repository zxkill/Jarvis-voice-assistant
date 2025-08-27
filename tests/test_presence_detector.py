from sensors.vision.presence import PresenceDetector


class DummyTracker:
    def __init__(self):
        self.calls = []

    def update(self, detected, dx, dy, dt_ms):
        self.calls.append((detected, dx, dy, dt_ms))


def test_presence_events_and_tracker(monkeypatch):
    """При появлении/исчезновении лица публикуется событие и вызывается трекер."""

    events = []
    monkeypatch.setattr("sensors.vision.presence.publish", lambda e: events.append(e))

    tracker = DummyTracker()
    det = PresenceDetector(alpha=1.0, threshold=0.5, tracker=tracker)

    det.update(True, 5.0, -3.0, 33)
    assert tracker.calls[-1] == (True, 5.0, -3.0, 33)
    assert events[-1].kind == "presence.update" and events[-1].attrs["present"] is True

    det.update(False, 0.0, 0.0, 33)
    assert tracker.calls[-1][0] is False
    assert events[-1].attrs["present"] is False
