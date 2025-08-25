import json
import time
import types

from display import DisplayItem
from display.drivers.serial import SerialDisplayDriver


class DummySerial:
    def __init__(self):
        self.buffer = bytearray()
        self.written = []
        self.is_open = True

    @property
    def in_waiting(self):
        return len(self.buffer)

    def read(self, n):
        if not self.buffer:
            time.sleep(0.01)
            return b""
        chunk = self.buffer[:n]
        del self.buffer[:n]
        return bytes(chunk)

    def write(self, data):
        self.written.append(data.decode())

    def close(self):
        self.is_open = False

    def feed(self, data: bytes):
        self.buffer.extend(data)


def test_serial_handshake_resends_cache(monkeypatch):
    dummy = DummySerial()
    fake_serial = types.SimpleNamespace(Serial=lambda *a, **k: dummy, SerialException=Exception)
    monkeypatch.setattr("display.drivers.serial.serial", fake_serial)

    driver = SerialDisplayDriver(port="dummy")
    item = DisplayItem(kind="txt", payload="hi")
    driver.draw(item)
    dummy.written.clear()

    dummy.feed(b'{"kind":"hello","payload":"ready"}\n')
    assert driver.wait_ready(timeout=1.0)
    time.sleep(0.1)
    driver.close()

    assert any(json.loads(w)["kind"] == "txt" for w in dummy.written)
