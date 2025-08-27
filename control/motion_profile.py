"""Профиль движения с ограничением ускорения и джерка.

Класс :class:`MotionProfile` реализует дискретное вычисление скорости и
ускорения с учётом предельных значений ускорения и рывка (джерка).
Это полезно для плавного управления сервоприводами и колёсными
платформами.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Tuple

logger = logging.getLogger(__name__)


def _clamp(value: float, limits: Tuple[float, float]) -> float:
    low, high = limits
    if value < low:
        return low
    if value > high:
        return high
    return value


@dataclass
class MotionProfile:
    """Простой профиль движения с ограничением ускорения и джерка.

    Parameters
    ----------
    max_acceleration: максимальное допустимое ускорение (ед/с^2).
    max_jerk: максимальное изменение ускорения за секунду (ед/с^3).
    """

    max_acceleration: float
    max_jerk: float

    def __post_init__(self) -> None:
        self.velocity = 0.0
        self.acceleration = 0.0
        logger.debug(
            "MotionProfile инициализирован: max_acc=%.3f max_jerk=%.3f",
            self.max_acceleration,
            self.max_jerk,
        )

    def reset(self) -> None:
        """Сбрасывает текущую скорость и ускорение."""
        self.velocity = 0.0
        self.acceleration = 0.0
        logger.debug("MotionProfile сброшен")

    def update(self, target_velocity: float, dt: float) -> Tuple[float, float, float]:
        """Обновляет профиль на шаг ``dt``.

        Возвращает кортеж ``(velocity, acceleration, jerk)``.
        """

        # Требуемое ускорение для достижения целевой скорости
        desired_acc = (target_velocity - self.velocity) / dt

        # Вычисляем джерк, ограничивая его
        jerk = desired_acc - self.acceleration
        jerk_limit = self.max_jerk * dt
        clamped_jerk = _clamp(jerk, (-jerk_limit, jerk_limit))
        if clamped_jerk != jerk:
            logger.debug("Jerk clamped: %.3f -> %.3f", jerk, clamped_jerk)
        jerk = clamped_jerk
        self.acceleration += jerk

        # Ограничиваем ускорение
        before_acc = self.acceleration
        self.acceleration = _clamp(
            self.acceleration, (-self.max_acceleration, self.max_acceleration)
        )
        if self.acceleration != before_acc:
            logger.debug(
                "Acceleration clamped: %.3f -> %.3f", before_acc, self.acceleration
            )

        # Интегрируем скорость
        self.velocity += self.acceleration * dt

        logger.debug(
            "MotionProfile step: target_v=%.3f v=%.3f a=%.3f jerk=%.3f",
            target_velocity,
            self.velocity,
            self.acceleration,
            jerk,
        )
        return self.velocity, self.acceleration, jerk
