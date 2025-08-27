"""Простая детекция присутствия пользователя по факту обнаружения лица.

Модуль не занимается управлением сервоприводами — эту задачу выполняет
`face_tracker`.  Здесь лишь оценивается вероятность присутствия и при
смене состояния публикуется событие ``presence.update``.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

from core.events import Event, publish
from core.logging_json import configure_logging
from .face_tracker import FaceTracker

log = configure_logging(__name__)


@dataclass
class PresenceState:
    """Состояние присутствия в кадре."""

    present: bool = False
    confidence: float = 0.0
    last_seen: float = 0.0
    last_kind: Optional[str] = None


class PresenceDetector:
    """EMA-для оценки присутствия лица и запуск `FaceTracker`."""

    def __init__(self, alpha: float = 0.6, threshold: float = 0.5, tracker: Optional[FaceTracker] = None) -> None:
        # Коэффициент сглаживания и порог появления/исчезновения
        self.alpha = alpha
        self.threshold = threshold
        # Текущее состояние
        self.state = PresenceState()
        # Трекер лица, отвечающий за поворот камеры
        self.tracker = tracker or FaceTracker()

    # ------------------------------------------------------------------
    def update(self, detected: bool, dx_px: float = 0.0, dy_px: float = 0.0, dt_ms: int = 0) -> None:
        """Обновить внутреннее состояние на основе результата детекции.

        :param detected: найдено ли лицо на кадре
        :param dx_px: смещение по горизонтали от центра кадра
        :param dy_px: смещение по вертикали
        :param dt_ms: время с прошлого кадра; пробрасывается в трекер
        """

        # Переводим факт обнаружения в числовое значение и сглаживаем
        value = 1.0 if detected else 0.0
        self.state.confidence = self.state.confidence * (1 - self.alpha) + value * self.alpha

        if detected:
            # Запоминаем время и тип последнего объекта
            self.state.last_seen = time.monotonic()
            self.state.last_kind = "face"

        previous = self.state.present
        self.state.present = self.state.confidence >= self.threshold

        if self.state.present != previous:
            # Публикуем событие при смене состояния
            publish(
                Event(
                    kind="presence.update",
                    attrs={"present": self.state.present, "confidence": self.state.confidence},
                )
            )
            log.info(
                "Presence %s (confidence=%.2f)",
                "detected" if self.state.present else "lost",
                self.state.confidence,
            )
        else:
            log.debug("confidence=%.2f detected=%s", self.state.confidence, detected)

        # Передаём данные в трекер — он сам решит, нужно ли двигать камеру
        if detected:
            self.tracker.update(True, dx_px, dy_px, dt_ms)
        else:
            self.tracker.update(False, 0.0, 0.0, dt_ms)


__all__ = ["PresenceDetector", "PresenceState"]
