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

    import importlib
    import display.drivers.serial as serial_module
    importlib.reload(serial_module)
    import importlib
    import display.drivers.serial as serial_module
    importlib.reload(serial_module)
    from display.drivers.serial import SerialDisplayDriver

    driver = SerialDisplayDriver(port="dummy")
    item = DisplayItem(kind="txt", payload="hi")
    driver.draw(item)
    dummy.written.clear()

    dummy.feed(b'{"kind":"hello","payload":"ready"}\n')
    driver.wait_ready(timeout=2.0)
    time.sleep(0.2)
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


def test_write_failures_reset_after_reconnect(monkeypatch):
    """Счётчик ошибок записи сбрасывается после переподключения."""

    class WriteTimeout(Exception):
        pass

    class FailingSerial:
        def __init__(self):
            self.is_open = True

        def write(self, data):
            raise WriteTimeout("Write timeout")

        def close(self):
            self.is_open = False

    failing = FailingSerial()
    working = DummySerial()

    serial_objs = [failing, working]

    def serial_factory(*args, **kwargs):
        return serial_objs.pop(0)

    fake_tools = types.SimpleNamespace(list_ports=types.SimpleNamespace(comports=lambda: []))
    import display.drivers.serial as serial_module

    monkeypatch.setattr(
        serial_module,
        "serial",
        types.SimpleNamespace(Serial=serial_factory, SerialException=WriteTimeout),
    )
    monkeypatch.setattr(serial_module, "list_ports", fake_tools.list_ports)
    from display.drivers.serial import SerialDisplayDriver

    # Отключаем настоящий поток чтения, чтобы тест был синхронным
    monkeypatch.setattr(SerialDisplayDriver, "_reader", lambda self: None)

    driver = SerialDisplayDriver(port="dummy", max_write_failures=2)

    item = DisplayItem(kind="txt", payload="hi")
    for _ in range(2):
        driver.draw(item)

    assert driver.disconnected.is_set(), "Должно произойти отключение"
    assert driver._write_failures == 2, "Счётчик ошибок должен достигнуть порога"

    driver.disconnected.clear()
    driver._open_serial()
    assert driver._write_failures == 0, "Счётчик ошибок должен сбрасываться после переподключения"

    driver.draw(item)
    assert working.written, "После переподключения запись должна проходить"

    driver.close()


def test_no_duplicate_threshold_logs(monkeypatch):
    """После отключения счётчик ошибок не должен превышать порог."""

    class WriteTimeout(Exception):
        pass

    class FailingSerial:
        def __init__(self):
            self.is_open = True

        def write(self, data):
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

    # Отключаем реальный поток чтения, чтобы избежать гонок в тесте
    monkeypatch.setattr(SerialDisplayDriver, "_reader", lambda self: None)
    monkeypatch.setattr(
        SerialDisplayDriver,
        "_open_serial",
        lambda self, timeout=None: setattr(self, "ser", dummy),
    )

    driver = SerialDisplayDriver(port="dummy", max_write_failures=3)
    item = DisplayItem(kind="txt", payload="hi")

    for _ in range(5):  # попыток больше порога
        driver.draw(item)

    # Счётчик должен остановиться на пороговом значении, даже если попыток было больше
    assert driver._write_failures == driver.max_write_failures
    assert driver.disconnected.is_set()

    driver.close()


def test_non_json_lines_are_ignored(monkeypatch, capfd):
    """Строки без JSON должны игнорироваться и не увеличивать счётчик ошибок."""

    dummy = DummySerial()
    fake_serial = types.SimpleNamespace(Serial=lambda *a, **k: dummy, SerialException=Exception)
    fake_tools = types.SimpleNamespace(list_ports=types.SimpleNamespace(comports=lambda: []))
    fake_serial.tools = fake_tools
    monkeypatch.setitem(sys.modules, "serial", fake_serial)
    monkeypatch.setitem(sys.modules, "serial.tools", fake_tools)
    monkeypatch.setitem(sys.modules, "serial.tools.list_ports", fake_tools.list_ports)

    from display.drivers.serial import SerialDisplayDriver

    driver = SerialDisplayDriver(port="dummy")
    # Подбрасываем в буфер строку без фигурных скобок
    dummy.feed(b"[I] [SER] kind='track'\n")
    time.sleep(0.3)

    # Считываем логи до закрытия и после, чтобы ничего не потерять
    err = capfd.readouterr().err
    driver.close()
    err += capfd.readouterr().err

    # Драйвер должен проигнорировать строку и не записать событие в очередь
    assert driver._inq.empty(), "Очередь событий должна быть пустой"
    # В логах не должно появиться сообщения о некорректном JSON
    assert "Bad JSON" not in err, "Не должно быть ошибок JSON"


def test_driver_disables_serial_logging(monkeypatch):
    """После рукопожатия драйвер отправляет команду отключения логов."""

    dummy = DummySerial()
    fake_serial = types.SimpleNamespace(Serial=lambda *a, **k: dummy, SerialException=Exception)
    fake_tools = types.SimpleNamespace(list_ports=types.SimpleNamespace(comports=lambda: []))
    fake_serial.tools = fake_tools
    monkeypatch.setitem(sys.modules, "serial", fake_serial)
    monkeypatch.setitem(sys.modules, "serial.tools", fake_tools)
    monkeypatch.setitem(sys.modules, "serial.tools.list_ports", fake_tools.list_ports)

    import importlib
    import display.drivers.serial as serial_module
    importlib.reload(serial_module)
    from display.drivers.serial import SerialDisplayDriver

    sent: list[tuple[str, str]] = []
    orig_send = SerialDisplayDriver._send_json

    def capture(self, kind, payload):
        sent.append((kind, payload))
        orig_send(self, kind, payload)

    monkeypatch.setattr(SerialDisplayDriver, "_send_json", capture)

    driver = SerialDisplayDriver(port="dummy")

    dummy.feed(b'{"kind":"hello","payload":"ready"}\n')
    driver.wait_ready(timeout=2.0)
    time.sleep(0.1)
    driver.close()

    assert ("log", "off") in sent, "Должна быть отправлена команда log=off"


def test_parse_json_line_recovers_missing_quotes():
    """Парсер должен восстанавливать ключи без кавычек."""
    from display.drivers.serial import _parse_json_line

    msg = _parse_json_line('{kind":"hello","payload":"ping"}')
    assert msg == {"kind": "hello", "payload": "ping"}


def test_parse_json_line_recovers_missing_braces():
    """Парсер должен восстанавливать потерянные фигурные скобки."""
    from display.drivers.serial import _parse_json_line

    msg1 = _parse_json_line('kind":"hello","payload":"ping"}')
    msg2 = _parse_json_line('{"kind":"hello","payload":"ping"')
    assert msg1 == {"kind": "hello", "payload": "ping"}
    assert msg2 == {"kind": "hello", "payload": "ping"}


def test_parse_json_line_strips_noise():
    """Парсер корректно вырезает мусор вокруг JSON."""
    from display.drivers.serial import _parse_json_line

    msg = _parse_json_line('xxx{"kind":"hello","payload":"ready"}yyy')
    assert msg == {"kind": "hello", "payload": "ready"}


def test_parse_json_line_returns_none_on_failure():
    """При невозможности парсинга возвращается None."""
    from display.drivers.serial import _parse_json_line

    assert _parse_json_line('broken') is None
