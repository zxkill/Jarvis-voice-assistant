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
    det = PresenceDetector(alpha=1.0, threshold=0.5, tracker=tracker, show_window=False)

    det.update(True, 5.0, -3.0, 33)
    assert tracker.calls[-1] == (True, 5.0, -3.0, 33)
    assert events[-1].kind == "presence.update" and events[-1].attrs["present"] is True

    det.update(False, 0.0, 0.0, 33)
    assert tracker.calls[-1][0] is False
    assert events[-1].attrs["present"] is False


def test_run_without_camera_index():
    """Метод run() без указания камеры завершается без ошибок."""

    det = PresenceDetector(show_window=False)
    det.run()  # просто проверяем, что метод не выбрасывает исключений


def test_run_without_cv2(monkeypatch):
    """Если OpenCV отсутствует, run() должен завершиться сразу."""

    monkeypatch.setattr("sensors.vision.presence.cv2", None)
    det = PresenceDetector(camera_index=0, show_window=False)
    det.run()  # отсутствие зависимостей не должно приводить к исключениям
