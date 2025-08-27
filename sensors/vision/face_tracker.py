"""Трекинг лица с публикацией событий.

Модуль отделён от детекции присутствия.  На вход подаются смещения
центра лица относительно центра кадра, а на выходе получаем
сглаженные значения, которые отправляются сервоприводам и
публикуются через ``core.events``.  Сглаживание выполняется методом
экспоненциального среднего для устойчивого поведения камеры.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from core.events import Event, publish
from core.logging_json import configure_logging

# Попытка импортировать драйвер дисплея. В тестовой среде его может не быть,
# поэтому оборачиваем импорт в try/except.
try:  # pragma: no cover - в тестах драйвер отсутствует
    from display import get_driver, DisplayItem
except Exception as exc:  # pylint: disable=broad-except
    get_driver = None  # type: ignore
    DisplayItem = None  # type: ignore
    logging.getLogger(__name__).warning("Display driver isn't available: %s", exc)


log = configure_logging(__name__)

# ---------------------------------------------------------------------------
# Вспомогательные функции работы с драйвером поворота камеры
_driver = None
_tracking_active = False


def _send_track(dx_px: float, dy_px: float, dt_ms: int) -> None:
    """Отправить команду трекинга на дисплей/серводрайвер.

    Если драйвер недоступен, команда тихо игнорируется. Подробные
    отладочные сообщения помогают понять, что именно происходит.
    """

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
    try:
        pkt = DisplayItem(
            kind="track",
            payload={"dx_px": round(dx_px, 1), "dy_px": round(dy_px, 1), "dt_ms": int(dt_ms)},
        )
        _driver.draw(pkt)
    except Exception as exc:  # pylint: disable=broad-except
        log.exception("Ошибка отправки WS(track): %s", exc)


def _clear_track() -> None:
    """Сбросить команду трекинга, останавливая сервоприводы."""

    global _driver  # pylint: disable=global-statement
    if _driver is None and get_driver:
        try:
            _driver = get_driver()
        except Exception as exc:  # pylint: disable=broad-except
            log.debug("driver not ready: %s", exc)
            return
    if not _driver:
        return
    try:
        _driver.draw(DisplayItem(kind="track", payload=None))
        log.debug("WS → track cleared")
    except Exception as exc:  # pylint: disable=broad-except
        log.exception("Ошибка отправки WS(track clear): %s", exc)


def _update_track(detected: bool, dx_px: float, dy_px: float, dt_ms: int) -> None:
    """Отправить координаты слежения или остановить сервоприводы."""

    global _tracking_active  # pylint: disable=global-statement
    if detected:
        _send_track(dx_px, dy_px, dt_ms)
        _tracking_active = True
    elif _tracking_active:
        _clear_track()
        _tracking_active = False


# ---------------------------------------------------------------------------
@dataclass
class FaceTracker:
    """Алгоритм сглаживания координат лица.

    Параметр ``alpha`` определяет степень влияния новой измеренной точки.
    Значение 1.0 отключает сглаживание, 0.0 — полностью игнорирует новые
    значения (фиксирует камеру).  При каждом обновлении публикуется событие
    ``face_tracker.update`` для других компонентов системы.
    """

    alpha: float = 0.5
    dx: float = 0.0
    dy: float = 0.0

    def update(self, detected: bool, dx_px: float = 0.0, dy_px: float = 0.0, dt_ms: int = 0) -> None:
        """Обновить состояние трекера.

        :param detected: обнаружено ли лицо на текущем кадре
        :param dx_px: смещение центра лица по горизонтали относительно центра кадра
        :param dy_px: смещение центра лица по вертикали
        :param dt_ms: время с предыдущего кадра в миллисекундах
        """

        if detected:
            # Экспоненциальное сглаживание для устойчивого движения серв
            self.dx += self.alpha * (dx_px - self.dx)
            self.dy += self.alpha * (dy_px - self.dy)
            log.debug("track update dx=%.1f dy=%.1f", self.dx, self.dy)
            _update_track(True, self.dx, self.dy, dt_ms)
            publish(
                Event(
                    kind="face_tracker.update",
                    attrs={"detected": True, "dx": self.dx, "dy": self.dy, "dt_ms": dt_ms},
                )
            )
        else:
            # Лицо потеряно — сообщаем об этом и сбрасываем приводы
            log.debug("track lost")
            _update_track(False, 0.0, 0.0, dt_ms)
            self.dx = self.dy = 0.0
            publish(Event(kind="face_tracker.update", attrs={"detected": False}))


__all__ = ["FaceTracker"]
