import types

from sensors.vision import presence


class DummyDriver:
    def __init__(self):
        self.calls = []

    def draw(self, item):
        self.calls.append(item)


def test_update_track_stops_when_lost(monkeypatch):
    driver = DummyDriver()
    monkeypatch.setattr(presence, "_driver", driver)
    monkeypatch.setattr(presence, "DisplayItem", lambda kind, payload: types.SimpleNamespace(kind=kind, payload=payload))
    presence._tracking_active = False

    presence._update_track(True, 5.0, -3.0, 40)
    assert driver.calls[-1].payload["dx_px"] == 5.0

    presence._update_track(False, 0.0, 0.0, 0)
    assert driver.calls[-1].payload is None

    call_count = len(driver.calls)
    presence._update_track(False, 0.0, 0.0, 0)
    assert len(driver.calls) == call_count
