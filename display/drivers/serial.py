from __future__ import annotations

import atexit
import json
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
    ) -> None:
        self.port = port or _find_default_port()
        self.baud = baud
        self.reconnect_delay = reconnect_delay
        self.startup_timeout = startup_timeout
        self._last: dict[str, DisplayItem] = {}
        self._inq: Queue[tuple[str, str]] = Queue()
        self._running = threading.Event()
        self._running.set()
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
        """Process incoming events from M5Stack."""
        try:
            while True:
                kind, payload = self._inq.get_nowait()
                self.on_event(kind, payload)
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

    def on_event(self, kind: str, payload: str) -> None:
        """Handle events from M5Stack. Override as needed."""
        log.debug("Event received kind=%s payload=%s", kind, payload)
        if kind == "hello":
            if payload == "ready":
                now = time.monotonic()
                # Ignore duplicate handshakes that arrive in quick succession
                if now - self._last_handshake < 5:
                    log.debug("Duplicate handshake ignored")
                else:
                    self._last_handshake = now
                    log.info("Handshake received; pushing cache")
                    self._cache_sent = False
                    self._push_cache()
                    self.ready.set()
            if payload in ("ready", "ping"):
                self._send_json("hello", "pong")
            return

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

    def _send_json(self, kind: str, payload: str) -> None:
        """Write an arbitrary JSON message to the serial port."""
        if not self.ser or not self.ser.is_open:
            log.debug("Serial port not open, dropping frame kind=%s", kind)
            return
        msg = json.dumps({"kind": kind, "payload": payload})
        log.debug("SER→M5 %s", msg)
        try:
            self.ser.write(msg.encode() + b"\n")
        except serial.SerialException as exc:
            log.error("Write error: %s", exc)

    def _send_item(self, item: DisplayItem) -> None:
        """Serialize item as JSON and write to serial port."""
        self._send_json(item.kind, item.payload)

    def _reader(self) -> None:
        """Background thread: read lines, parse JSON, enqueue events."""
        buf = b""
        while self._running.is_set():
            try:
                chunk = self.ser.read(self.ser.in_waiting or 1)
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

                    idx = line.find("{")
                    end = line.rfind("}")
                    if idx == -1 or end == -1:
                        continue
                    if idx > 0:
                        log.debug("Discarding %d bytes of noise before JSON", idx)
                        line = line[idx:]
                        end -= idx
                    if end < len(line) - 1:
                        log.debug("Discarding %d bytes of noise after JSON", len(line) - end - 1)
                        line = line[: end + 1]

                    try:
                        msg = json.loads(line)
                        kind = msg.get("kind", "")
                        payload = msg.get("payload", "")
                        self._inq.put((kind, payload))
                        self.on_event(kind, payload)
                    except json.JSONDecodeError:
                        log.error("Bad JSON from M5: %s", line)
            except (serial.SerialException, AttributeError) as exc:
                if not self._running.is_set():
                    break
                log.warning("Connection lost: %s", exc)
                self.disconnected.set()
                try:
                    if self.ser and self.ser.is_open:
                        self.ser.close()
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
