import json
import sys
import time
import types

from display import DisplayItem


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
    fake_tools = types.SimpleNamespace(list_ports=types.SimpleNamespace(comports=lambda: []))
    fake_serial.tools = fake_tools
    monkeypatch.setitem(sys.modules, "serial", fake_serial)
    monkeypatch.setitem(sys.modules, "serial.tools", fake_tools)
    monkeypatch.setitem(sys.modules, "serial.tools.list_ports", fake_tools.list_ports)

    from display.drivers.serial import SerialDisplayDriver

    driver = SerialDisplayDriver(port="dummy")
    item = DisplayItem(kind="txt", payload="hi")
    driver.draw(item)
    dummy.written.clear()

    dummy.feed(b'{"kind":"hello","payload":"ready"}\n')
    assert driver.wait_ready(timeout=1.0)
    time.sleep(0.1)
    driver.close()

    assert any(json.loads(w)["kind"] == "txt" for w in dummy.written)


def test_single_write_error_keeps_port_open(monkeypatch):
    """Одиночная ошибка записи не должна приводить к отключению."""

    class WriteTimeout(Exception):
        pass

    class FailingSerial:
        """Серийный порт, который всегда выбрасывает исключение при записи."""

        def __init__(self):
            self.is_open = True

        def write(self, data):  # noqa: D401 - описано выше
            raise WriteTimeout("Write timeout")

        def close(self):
            self.is_open = False

    dummy = FailingSerial()
    fake_tools = types.SimpleNamespace(list_ports=types.SimpleNamespace(comports=lambda: []))
    fake_serial = types.SimpleNamespace(SerialException=WriteTimeout, tools=fake_tools)
    monkeypatch.setitem(sys.modules, "serial", fake_serial)
    monkeypatch.setitem(sys.modules, "serial.tools", fake_tools)
    monkeypatch.setitem(sys.modules, "serial.tools.list_ports", fake_tools.list_ports)

    from display.drivers.serial import SerialDisplayDriver

    # Подменяем метод открытия порта, чтобы использовать наш фиктивный объект
    monkeypatch.setattr(
        SerialDisplayDriver,
        "_open_serial",
        lambda self, timeout=None: setattr(self, "ser", dummy),
    )

    driver = SerialDisplayDriver(port="dummy", max_write_failures=3)

    item = DisplayItem(kind="txt", payload="hi")
    driver.draw(item)  # попытка записи вызовет исключение

    # После одной ошибки порт должен оставаться открытым и флаг не выставлен
    assert not driver.disconnected.is_set(), "Не должно произойти отключение после одной ошибки"
    assert dummy.is_open, "Порт не должен быть закрыт"

    driver.close()


def test_write_error_threshold_closes_port_and_sets_flag(monkeypatch):
    """После превышения порога ошибок записи порт закрывается и выставляется флаг disconnect."""

    class WriteTimeout(Exception):
        pass

    class FailingSerial:
        """Серийный порт, имитирующий ошибку записи."""

        def __init__(self):
            self.is_open = True

        def write(self, data):  # noqa: D401 - описано выше
            raise WriteTimeout("Write timeout")

        def close(self):
            self.is_open = False

    dummy = FailingSerial()
    fake_tools = types.SimpleNamespace(list_ports=types.SimpleNamespace(comports=lambda: []))
    fake_serial = types.SimpleNamespace(SerialException=WriteTimeout, tools=fake_tools)
    monkeypatch.setitem(sys.modules, "serial", fake_serial)
    monkeypatch.setitem(sys.modules, "serial.tools", fake_tools)
    monkeypatch.setitem(sys.modules, "serial.tools.list_ports", fake_tools.list_ports)

    from display.drivers.serial import SerialDisplayDriver

    # Подменяем метод открытия порта, чтобы использовать наш фиктивный объект
    monkeypatch.setattr(
        SerialDisplayDriver,
        "_open_serial",
        lambda self, timeout=None: setattr(self, "ser", dummy),
    )

    driver = SerialDisplayDriver(port="dummy", max_write_failures=3)

    item = DisplayItem(kind="txt", payload="hi")
    # Совершаем несколько попыток записи, чтобы превысить порог ошибок
    for _ in range(3):
        driver.draw(item)

    assert driver.disconnected.is_set(), "Флаг отключения должен быть установлен после N ошибок"
    assert not dummy.is_open, "Порт должен быть закрыт после превышения порога"

    driver.close()


def test_parse_json_line_recovers_missing_quotes():
    """Парсер должен восстанавливать ключи без кавычек."""
    from display.drivers.serial import _parse_json_line

    msg = _parse_json_line('{kind":"hello","payload":"ping"}')
    assert msg == {"kind": "hello", "payload": "ping"}


def test_parse_json_line_strips_noise():
    """Парсер корректно вырезает мусор вокруг JSON."""
    from display.drivers.serial import _parse_json_line

    msg = _parse_json_line('xxx{"kind":"hello","payload":"ready"}yyy')
    assert msg == {"kind": "hello", "payload": "ready"}


def test_parse_json_line_returns_none_on_failure():
    """При невозможности парсинга возвращается None."""
    from display.drivers.serial import _parse_json_line

    assert _parse_json_line('broken') is None
