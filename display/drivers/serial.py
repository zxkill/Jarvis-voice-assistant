from __future__ import annotations

import atexit
import json
import re
import threading
import time
from queue import Empty, Queue
from typing import Optional

import serial
from serial.tools import list_ports

from core.logging_json import configure_logging

from display import DisplayDriver, DisplayItem

log = configure_logging("display.serial")


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------

def _parse_json_line(line: str) -> dict | None:
    """
    Попытаться восстановить и распарсить JSON, пришедший от M5Stack.

    1. Отбрасывает мусор до первой фигурной скобки и после последней.
    2. Добавляет кавычки вокруг ключей, если они были утеряны в потоке.

    Возвращает словарь при успехе или ``None`` при ошибке.
    """

    # Находим границы возможного JSON. Иногда прошивка теряет фигурные скобки
    # в начале или конце строки, поэтому пытаемся восстановить их.
    idx = line.find("{")
    end = line.rfind("}")

    if idx == -1 and end != -1:
        # Потеряна открывающая скобка: добавляем её и обрезаем строку.
        clean = "{" + line[: end + 1]
    elif idx != -1 and end == -1:
        # Потеряна закрывающая скобка: добавляем её в конец.
        clean = line[idx:] + "}"
    elif idx == -1 or end == -1 or end <= idx:
        # Скобки полностью отсутствуют или расположены некорректно.
        return None
    else:
        clean = line[idx : end + 1]

    if clean != line:
        # Логируем восстановленную строку для упрощения отладки.
        log.debug("JSON recovered: %s -> %s", line, clean)

    # Добавляем кавычки вокруг ключей, если они потерялись в потоке
    if clean.startswith("{") and not clean.startswith('{"'):
        clean = '{"' + clean[1:]
    clean = re.sub(r',\s*([A-Za-z_][A-Za-z0-9_]*)"?\s*:', r',"\1":', clean)

    try:
        return json.loads(clean)
    except json.JSONDecodeError:
        return None

# -----------------------------------------------------------------------------
# Helper – autodetect serial port
# -----------------------------------------------------------------------------

def _find_default_port() -> str:
    """Return the first serial port that looks like an M5Stack bridge."""
    from sys import platform

    if platform.startswith("linux"):
        return "/dev/ttyUSB0"
    if platform.startswith("darwin"):
        return "/dev/cu.SLAB_USBtoUART"

    # Windows: choose first COM port with known VID/PID
    VIDS_PIDS = {(0x10C4, 0xEA60), (0x1A86, 0x7523), (0x1A86, 0x55D4)}
    for p in list_ports.comports():
        if (p.vid, p.pid) in VIDS_PIDS:
            return p.device  # pragma: no cover
    log.error("M5Stack USB‑UART bridge not found. Please specify port explicitly.")
    raise RuntimeError("M5Stack USB‑UART not found")

# -----------------------------------------------------------------------------
# Driver
# -----------------------------------------------------------------------------

class SerialDisplayDriver(DisplayDriver):
    """Display driver that communicates with M5Stack over USB serial."""

    def __init__(
        self,
        port: Optional[str] = None,
        baud: int = 921_600,
        reconnect_delay: float = 2.0,
        startup_timeout: float = 5.0,
        max_write_failures: int = 3,
    ) -> None:
        self.port = port or _find_default_port()
        self.baud = baud
        self.reconnect_delay = reconnect_delay
        self.startup_timeout = startup_timeout
        # Сколько последовательных ошибок записи допускается, прежде чем
        # соединение будет считаться потерянным и потребуется переподключение
        self.max_write_failures = max_write_failures
        # Счётчик текущих ошибок записи подряд
        self._write_failures = 0
        self._last: dict[str, DisplayItem] = {}
        self._inq: Queue[tuple[str, str]] = Queue()
        self._running = threading.Event()
        self._running.set()
        # Блокировка на запись в порт, чтобы исключить одновременные обращения
        # из разных потоков и гонки при закрытии соединения
        self._tx_lock = threading.Lock()
        self.ser: Optional[serial.Serial] = None
        self._cache_sent = False
        self._last_handshake = 0.0
        self.ready = threading.Event()
        self.disconnected = threading.Event()
        self._open_serial(timeout=self.startup_timeout)  # blocks until port is opened
        atexit.register(self.close)

    def draw(self, item: DisplayItem) -> None:
        """Transmit *item* to display and cache it for reconnect."""
        if item.payload is None:
            self._last.pop(item.kind, None)
        else:
            self._last[item.kind] = item
        self._send_item(item)

    def forget(self, kind: str) -> None:
        """Удалить закэшированный элемент указанного вида."""
        self._last.pop(kind, None)

    def process_events(self) -> None:
        """Очистить очередь событий.

        После перехода на NDJSON все события обрабатываются сразу в потоке
        чтения.  Очередь используется лишь как отладочный буфер и очищается
        здесь, чтобы не накапливать элементы бесконечно.
        """

        try:
            while True:
                self._inq.get_nowait()
        except Empty:
            pass

    def _push_cache(self) -> None:
        """Resend cached frames once after the board signals readiness."""
        if self._cache_sent:
            log.debug("Cache already sent, skipping resend")
            return
        log.info("Resending %d cached frames", len(self._last))
        for it in self._last.values():
            self._send_item(it)
        self._cache_sent = True

    def on_event(self, line: str) -> bool:
        """Разобрать и обработать одну строку NDJSON, пришедшую по Serial.

        Возвращает ``True`` при успешной обработке и ``False`` при ошибке
        валидации.  Корректные события добавляются в очередь ``_inq`` для
        внешних наблюдателей.
        """

        msg = _parse_json_line(line)
        if msg is None:
            # Линия не похожа на JSON – логируем и отбрасываем
            log.error("Invalid NDJSON from M5: %s", line)
            return False

        kind = msg.get("kind")
        payload = msg.get("payload")
        if not isinstance(kind, str) or not isinstance(payload, str):
            # Если поля отсутствуют или имеют неожиданный тип – также отбрасываем
            log.error("Invalid NDJSON structure: %s", line)
            return False

        # Сохраняем разобранное событие для возможной обработки в других местах
        self._inq.put((kind, payload))
        log.debug("Event received kind=%s payload=%s", kind, payload)

        # Обработка рукопожатия и служебных сообщений
        if kind == "hello" and payload in ("ready", "ping"):
            now = time.monotonic()
            # Treat the first ping as a handshake if we missed the initial
            # "ready" message (e.g. Jarvis started after the board booted).
            if not self.ready.is_set() or payload == "ready":
                # Ignore duplicate handshakes that arrive in quick
                # succession, but allow a later "ready" to refresh cache.
                if now - self._last_handshake < 5 and self.ready.is_set():
                    log.debug("Duplicate handshake ignored")
                else:
                    self._last_handshake = now
                    log.info("Handshake received; pushing cache")
                    self._cache_sent = False
                    self._push_cache()
                    self.ready.set()
                    # После успешного рукопожатия просим устройство
                    # отключить вывод логов в USB Serial, чтобы канал
                    # использовался только для команд.
                    self._send_json("log", "off")
            self._send_json("hello", "pong")
        return True

    def _open_serial(self, timeout: float | None = None) -> None:
        """Open serial connection and start reader thread."""
        start = time.monotonic()
        while self._running.is_set():
            try:
                self.ser = serial.Serial(
                    self.port,
                    self.baud,
                    timeout=1,
                    dsrdtr=False,
                    rtscts=False,
                )
                log.info("Serial opened %s @%d", self.port, self.baud)
                self._cache_sent = False
                self.ready.clear()
                self.disconnected.clear()
                # После успешного открытия порта сбрасываем счётчик ошибок записи,
                # чтобы новые попытки начинались "с чистого листа".
                if self._write_failures:
                    log.debug("Reset write failure counter after reconnect")
                self._write_failures = 0
                break
            except serial.SerialException as exc:
                if timeout is not None and time.monotonic() - start >= timeout:
                    raise RuntimeError(f"Serial open failed: {exc}")
                log.warning(
                    "Serial open failed: %s. Retrying in %.1fs", exc, self.reconnect_delay
                )
                time.sleep(self.reconnect_delay)

        # Spawn reader thread
        self._rx = threading.Thread(target=self._reader, daemon=True)
        self._rx.start()

    def wait_ready(self, timeout: float = 5.0) -> bool:
        """Wait until board sends initial handshake."""
        return self.ready.wait(timeout)

    def _send_dict(self, msg: dict) -> None:
        """Отправить словарь как строку NDJSON с добавлением timestamp."""

        msg = {"ts": time.time(), **msg}
        with self._tx_lock:
            # Если порт уже закрыт, просто фиксируем факт и не пытаемся писать
            if not self.ser or not self.ser.is_open:
                log.debug(
                    "Serial port not open, dropping frame kind=%s", msg.get("kind")
                )
                return

            line = json.dumps(msg)
            log.debug("SER→M5 %s", line)
            try:
                self.ser.write(line.encode() + b"\n")
                # Если запись прошла успешно, сбрасываем счётчик ошибок
                if self._write_failures:
                    log.debug("Write recovered after %d failures", self._write_failures)
                self._write_failures = 0
            except serial.SerialException as exc:
                # Ошибка записи: увеличиваем счётчик и решаем, нужно ли разрывать соединение
                self._write_failures += 1
                log.warning(
                    "Write error %d/%d: %s",
                    self._write_failures,
                    self.max_write_failures,
                    exc,
                )
                if self._write_failures >= self.max_write_failures:
                    # Превышен порог допустимых ошибок – считаем соединение потерянным
                    log.error("Write error threshold reached, disconnecting")
                    self.disconnected.set()
                    try:
                        if self.ser and self.ser.is_open:
                            log.warning("Closing serial port due to write failure")
                            self.ser.close()
                    except Exception:
                        # Логируем полную трассировку ошибки закрытия
                        log.exception("Error closing serial port after write failure")
                    finally:
                        # Удаляем ссылку на закрытый порт, чтобы другие потоки
                        # не пытались писать в него и не получали ошибку
                        self.ser = None

    def _send_json(self, kind: str, payload: str) -> None:
        """Отправить произвольное сообщение с указанными ``kind`` и ``payload``."""
        self._send_dict({"kind": kind, "payload": payload})

    def _send_item(self, item: DisplayItem) -> None:
        """Сериализовать ``DisplayItem`` в NDJSON и отправить по Serial."""
        self._send_dict({"kind": item.kind, "payload": item.payload})

    def _reader(self) -> None:
        """Фоновый поток чтения строк NDJSON от устройства."""
        buf = b""
        bad_json_count = 0  # счётчик подряд идущих ошибок парсинга
        while self._running.is_set():
            try:
                s = self.ser
                if not s or not s.is_open:
                    # Порт недоступен: ждём переподключения и инициируем его
                    log.warning("Serial port not ready, waiting for reconnect")
                    self.disconnected.set()
                    time.sleep(self.reconnect_delay)
                    self._open_serial()
                    buf = b""
                    continue

                chunk = s.read(s.in_waiting or 1)
                if not chunk:
                    continue
                buf += chunk
                while b"\n" in buf:
                    raw, buf = buf.split(b"\n", 1)
                    if not raw:
                        continue
                    line = raw.decode("utf-8", errors="replace").strip()
                    log.debug("M5→SE %s", line)

                    if "=== Device booting ===" in line or "rst:" in line:
                        log.info("Board reboot detected")
                        self._cache_sent = False

                    # Линии без JSON считаем диагностикой прошивки и пропускаем
                    # Например, строки вида `[I] [SER] kind='track'`
                    if "{" not in line or "}" not in line:
                        log.info("M5 log: %s", line)
                        continue

                    # Пытаемся обработать строку как NDJSON. При ошибке увеличиваем
                    # счётчик и при достижении порога сообщаем о возможной
                    # рассинхронизации.
                    if not self.on_event(line):
                        bad_json_count += 1
                        log.debug("Raw bytes: %s", raw)
                        if bad_json_count == 5:
                            log.warning(
                                "Five consecutive JSON errors — waiting for resync"
                            )
                        continue

                    bad_json_count = 0
            except serial.SerialException as exc:
                if not self._running.is_set():
                    break
                log.warning("Connection lost: %s", exc)
                self.disconnected.set()
                try:
                    if self.ser and self.ser.is_open:
                        self.ser.close()
                    self.ser = None
                finally:
                    time.sleep(self.reconnect_delay)
                    self._open_serial()
                    buf = b""

    def close(self) -> None:
        """Clean up serial connection and reader thread."""
        if not self._running.is_set():
            return
        self._running.clear()
        try:
            if self.ser and self.ser.is_open:
                self.ser.close()
        except Exception:
            log.exception("Error closing serial port")
        if hasattr(self, "_rx") and self._rx.is_alive():
            self._rx.join(timeout=1)
        log.info("Serial closed %s", self.port)
