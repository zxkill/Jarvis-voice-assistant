import core.events as events
import sensors.vision.face_tracker as ft
from sensors.vision.face_tracker import FaceTracker
import pytest


class DummyDriver:
    def __init__(self):
        self.items = []

    def draw(self, item):  # pragma: no cover - тривиальный метод
        self.items.append(item)

    def process_events(self):  # pragma: no cover - не используется
        return


def _setup_driver(monkeypatch):
    driver = DummyDriver()
    monkeypatch.setattr(ft, "get_driver", lambda: driver)
    ft._driver = None
    ft._last_sent_ms = None
    ft._tracking_active = False
    return driver


def test_smoothing_and_events(monkeypatch):
    """Координаты сглаживаются EMA, события публикуются корректно."""
    _setup_driver(monkeypatch)
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
    assert captured[1].attrs["x"] == pytest.approx(0.5)
    assert captured[2].attrs["x"] == pytest.approx(0.25)
    assert captured[3].attrs == {"present": False}


def test_track_commands(monkeypatch):
    """Трекер отправляет корректные track-команды."""
    driver = _setup_driver(monkeypatch)
    tracker = FaceTracker(alpha=1.0)

    tracker.update((0.75, 0.25), 100, 50)
    # первая команда - движение к точке
    pkt = driver.items[-1]
    assert pkt.kind == "track"
    assert pkt.payload["dx_px"] == pytest.approx(25.0)
    assert pkt.payload["dy_px"] == pytest.approx(-12.5)

    tracker.update(None)
    # вторая команда - остановка
    assert driver.items[-1].payload is None
