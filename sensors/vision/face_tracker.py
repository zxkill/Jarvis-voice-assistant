from __future__ import annotations

"""Трекер лица с экспоненциальным сглаживанием координат.

При каждом обновлении публикует событие ``vision.face_tracker`` с текущим
положением лица.  Модуль не использует прямой доступ к камере и получает
координаты от ``PresenceDetector``.
"""

from dataclasses import dataclass
from typing import Optional, Tuple

from core.events import Event, publish
from core.logging_json import configure_logging

log = configure_logging(__name__)


@dataclass
class _State:
    """Внутреннее состояние трекера."""

    present: bool = False  # есть ли лицо в кадре
    x: float = 0.0  # сглаженная координата X (0..1)
    y: float = 0.0  # сглаженная координата Y (0..1)


class FaceTracker:
    """Сглаживает координаты лица и публикует события."""

    def __init__(self, alpha: float = 0.5) -> None:
        # Коэффициент EMA: 1.0 — мгновенное реагирование, 0.0 — отсутствие
        # обновлений. Оптимальное значение ~0.5 обеспечивает плавное движение.
        self.alpha = alpha
        self.state = _State()

    # ------------------------------------------------------------------
    def update(self, point: Optional[Tuple[float, float]]) -> None:
        """Обновляет положение лица.

        :param point: относительные координаты центра лица ``(x, y)``. Если
            ``None`` — лицо потеряно.
        """

        if point is None:
            if self.state.present:
                # Лицо исчезло из кадра — публикуем событие с флагом ``present``
                # и не изменяем последние координаты.
                self.state.present = False
                publish(Event(kind="vision.face_tracker", attrs={"present": False}))
                log.info("Лицо потеряно")
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
        log.debug("Сглаженные координаты лица: x=%.3f y=%.3f", self.state.x, self.state.y)
