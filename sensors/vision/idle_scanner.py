from __future__ import annotations

"""Фоновое сканирование камерой при отсутствии лица в кадре.

Класс :class:`IdleScanner` отслеживает события ``presence.update`` и
``speech.recognized``.  Если лицо не появляется в течение заданного
времени, камера начинает медленно сканировать пространство, отправляя
``track``-команды сервоприводам.  Найденное лицо или голосовая команда
останавливают сканирование.  Если во время обзора лица не обнаружено,
публикуется событие ``vision.sleep``, и камера «засыпает» на заданный
интервал, после чего цикл повторяется.
"""

import threading
import time

from core.events import Event, publish, subscribe
from core.logging_json import configure_logging
from control.scan_patterns import idle_scan
from sensors.vision.face_tracker import _send_track, _clear_track


log = configure_logging(__name__)


class IdleScanner:
    """Логика обзора камеры при отсутствии пользователя."""

    def __init__(
        self,
        *,
        idle_sec: float = 15.0,
        scan_sec: float = 30.0,
        sleep_sec: float = 60.0,
        step_ms: int = 200,
        frame_width: float = 100.0,
        frame_height: float = 50.0,
    ) -> None:
        # Временные параметры
        self.idle_sec = idle_sec
        self.scan_sec = scan_sec
        self.sleep_sec = sleep_sec
        self.step_ms = step_ms
        # Параметры кадра для расчёта dx/dy
        self.frame_width = frame_width
        self.frame_height = frame_height

        # Последний момент, когда лицо было в кадре
        self._last_seen = time.monotonic()
        # Флаги состояния
        self._stop_evt = threading.Event()
        self._scanner_thread = threading.Thread(target=self._loop, daemon=True)
        self._scanning = False
        self._sleeping = False

        subscribe("presence.update", self._on_presence)
        subscribe("speech.recognized", self._on_wakeup)
        self._scanner_thread.start()
        log.debug("IdleScanner запущен: idle=%.1f scan=%.1f sleep=%.1f", idle_sec, scan_sec, sleep_sec)

    # ------------------------------------------------------------------
    def _on_presence(self, event: Event) -> None:
        """Обработчик события присутствия."""

        if event.attrs.get("present"):
            self._last_seen = time.monotonic()
            log.info("Лицо обнаружено — останавливаем idle-сканирование")
        # При появлении лица или в любом случае прекращаем сканирование
        self._scanning = False

    # ------------------------------------------------------------------
    def _on_wakeup(self, _: Event) -> None:
        """Обработчик голосовой команды — выводит камеру из сна."""

        self._last_seen = time.monotonic()
        self._sleeping = False
        self._scanning = False
        log.info("Получена голосовая команда — пробуждение")

    # ------------------------------------------------------------------
    def _loop(self) -> None:
        """Основной цикл фонового сканирования."""

        while not self._stop_evt.is_set():
            now = time.monotonic()
            # Условие запуска сканирования
            if (
                not self._scanning
                and not self._sleeping
                and now - self._last_seen > self.idle_sec
            ):
                self._run_scan()
                self._last_seen = time.monotonic()
            time.sleep(0.1)

    # ------------------------------------------------------------------
    def _run_scan(self) -> None:
        """Запустить цикл сканирования и возможного сна."""

        self._scanning = True
        log.info("Начинаем обзор помещения")
        steps = max(1, int(self.scan_sec * 1000 / self.step_ms))
        pattern_x = idle_scan("sine", steps, amplitude=self.frame_width / 2)
        pattern_y = idle_scan("sine", steps, amplitude=self.frame_height / 4, frequency=0.5)

        start = time.monotonic()
        for dx, dy in zip(pattern_x, pattern_y):
            if not self._scanning:
                log.debug("Сканирование прервано")
                _clear_track()
                return
            _send_track(dx, dy, self.step_ms)
            time.sleep(self.step_ms / 1000.0)
            if time.monotonic() - self._last_seen < self.idle_sec:
                log.debug("Лицо найдено во время обзора")
                _clear_track()
                self._scanning = False
                return
        # Если дошли до конца паттерна и лица нет — уходим в сон
        _clear_track()
        self._scanning = False
        publish(Event(kind="vision.sleep", attrs={}))
        log.info("Лицо не найдено — сон на %.1f сек", self.sleep_sec)
        self._sleeping = True
        end = start + self.scan_sec + self.sleep_sec
        while time.monotonic() < end:
            if time.monotonic() - self._last_seen < self.idle_sec:
                log.info("Пробуждение во время сна")
                self._sleeping = False
                return
            if self._stop_evt.wait(0.1):
                return
        self._sleeping = False

    # ------------------------------------------------------------------
    def stop(self) -> None:
        """Остановить поток сканирования."""

        self._stop_evt.set()
        self._scanning = False
        self._sleeping = False
        _clear_track()
        self._scanner_thread.join(timeout=1.0)
        log.debug("IdleScanner остановлен")
