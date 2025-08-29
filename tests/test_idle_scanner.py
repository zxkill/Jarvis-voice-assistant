"""Тесты модуля idle_scanner."""

import time
import pytest

from sensors.vision.idle_scanner import IdleScanner
from sensors.vision import face_tracker as ft
from core import events
from core.events import Event


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
    return driver


def test_scanner_stops_on_face(monkeypatch):
    """Сканер запускается и останавливается при появлении лица."""
    driver = _setup_driver(monkeypatch)
    events._subscribers.clear()
    events._global_subscribers.clear()

    scanner = IdleScanner(idle_sec=0.05, scan_sec=0.2, sleep_sec=0.2, step_ms=20)
    for _ in range(20):
        if any(it.payload for it in driver.items):
            break
        time.sleep(0.01)
    else:
        pytest.fail("scan did not start")

    events.publish(Event(kind="presence.update", attrs={"present": True}))
    time.sleep(0.05)
    assert driver.items[-1].payload is None

    scanner.stop()
    events._subscribers.clear()
    events._global_subscribers.clear()


def test_scanner_goes_sleep(monkeypatch):
    """При отсутствии лица публикуется событие vision.sleep."""
    driver = _setup_driver(monkeypatch)
    events._subscribers.clear()
    events._global_subscribers.clear()
    captured = []
    events.subscribe("vision.sleep", captured.append)

    scanner = IdleScanner(idle_sec=0.05, scan_sec=0.1, sleep_sec=0.1, step_ms=20)
    time.sleep(0.3)
    assert captured and captured[0].kind == "vision.sleep"

    scanner.stop()
    events._subscribers.clear()
    events._global_subscribers.clear()
