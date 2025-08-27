from __future__ import annotations

"""Трекер лица с экспоненциальным сглаживанием координат.

Помимо публикации события ``vision.face_tracker`` отправляет команды
сервоприводам камеры через драйвер дисплея (``DisplayItem(kind="track")``),
что позволяет физически поворачивать камеру вслед за пользователем.
Модуль не использует прямой доступ к камере и получает координаты от
``PresenceDetector``.
"""

import time
from dataclasses import dataclass
from typing import Optional, Tuple

from core.events import Event, publish
from core.logging_json import configure_logging

# Драйвер дисплея нужен для команд поворота камеры. В тестовой среде он может
# отсутствовать, поэтому импортируем с защитой.
try:  # pragma: no cover - в тестах драйвер может не использоваться
    from display import DisplayItem, get_driver
except Exception:  # pylint: disable=broad-except
    DisplayItem = None  # type: ignore
    get_driver = None  # type: ignore

log = configure_logging(__name__)

# Внутренние переменные для отправки track-команд
_driver = None
_last_sent_ms: float | None = None
_tracking_active = False


@dataclass
class _State:
    """Внутреннее состояние трекера."""

    present: bool = False  # есть ли лицо в кадре
    x: float = 0.0  # сглаженная координата X (0..1)
    y: float = 0.0  # сглаженная координата Y (0..1)


class FaceTracker:
    """Сглаживает координаты лица, публикует события и отправляет track."""

    def __init__(self, alpha: float = 0.5) -> None:
        # Коэффициент EMA: 1.0 — мгновенное реагирование, 0.0 — отсутствие
        # обновлений. Оптимальное значение ~0.5 обеспечивает плавное движение.
        self.alpha = alpha
        self.state = _State()

    # ------------------------------------------------------------------
    def update(self, point: Optional[Tuple[float, float]],
               frame_width: float = 1.0,
               frame_height: float = 1.0) -> None:
        """Обновляет положение лица и управляет сервоприводами.

        :param point: относительные координаты центра лица ``(x, y)``.
            Значения находятся в диапазоне 0..1.  Если ``None`` — лицо
            потеряно и серво необходимо остановить.
        :param frame_width: ширина кадра в пикселях (для track-команды).
        :param frame_height: высота кадра в пикселях.
        """

        global _last_sent_ms, _tracking_active  # pylint: disable=global-statement

        if point is None:
            if self.state.present:
                self.state.present = False
                publish(Event(kind="vision.face_tracker", attrs={"present": False}))
                log.info("Лицо потеряно")
                _clear_track()
                _tracking_active = False
            return

        x, y = point
        if self.state.present:
            # Сглаживание: новое значение зависит от предыдущего.
            self.state.x = self.alpha * x + (1 - self.alpha) * self.state.x
            self.state.y = self.alpha * y + (1 - self.alpha) * self.state.y
        else:
            # Первое обнаружение — принимаем координаты без сглаживания.
            self.state.present = True
            self.state.x, self.state.y = x, y

        attrs = {"present": True, "x": self.state.x, "y": self.state.y}
        publish(Event(kind="vision.face_tracker", attrs=attrs))
        log.debug("Сглаженные координаты лица: x=%.3f y=%.3f",
                  self.state.x, self.state.y)

        # Подготовка и отправка команды поворота
        now_ms = time.monotonic() * 1000
        dt_ms = 0 if _last_sent_ms is None else now_ms - _last_sent_ms
        _last_sent_ms = now_ms
        dx_px = (self.state.x - 0.5) * frame_width
        dy_px = (self.state.y - 0.5) * frame_height
        _send_track(dx_px, dy_px, int(dt_ms))
        _tracking_active = True


def _send_track(dx_px: float, dy_px: float, dt_ms: int) -> None:
    """Отправить команду слежения драйверу дисплея."""

    global _driver  # pylint: disable=global-statement
    if _driver is None and get_driver:
        try:
            _driver = get_driver()
        except Exception as exc:  # pylint: disable=broad-except
            log.debug("driver not ready: %s", exc)
            return
    if not _driver:
        log.debug("driver missing: track dx=%+.1f dy=%+.1f", dx_px, dy_px)
        return
    try:  # pragma: no cover - в тестах исключения не ожидаются
        pkt = DisplayItem(kind="track",
                          payload={"dx_px": round(dx_px, 1),
                                   "dy_px": round(dy_px, 1),
                                   "dt_ms": dt_ms})
        _driver.draw(pkt)
        log.debug("WS → track dx=%+.1f dy=%+.1f dt=%d", dx_px, dy_px, dt_ms)
    except Exception as exc:  # pylint: disable=broad-except
        log.exception("Ошибка отправки WS(track): %s", exc)


def _clear_track() -> None:
    """Отправить команду остановки трекинга."""

    global _driver, _last_sent_ms  # pylint: disable=global-statement
    _last_sent_ms = None
    if _driver is None and get_driver:
        try:
            _driver = get_driver()
        except Exception as exc:  # pylint: disable=broad-except
            log.debug("driver not ready: %s", exc)
            return
    if not _driver:
        return
    try:  # pragma: no cover - в тестах исключения не ожидаются
        _driver.draw(DisplayItem(kind="track", payload=None))
        log.debug("WS → track cleared")
    except Exception as exc:  # pylint: disable=broad-except
        log.exception("Ошибка отправки WS(track clear): %s", exc)

