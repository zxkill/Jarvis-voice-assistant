"""PID контроллер с антивиндапом и ограничением скорости.

Модуль содержит класс :class:`PID`, реализующий пропорционально-
интегрально-дифференциальный регулятор. В отличие от простых
вариантов, здесь предусмотрены:

* **Антивиндап** — ограничение накопленной интегральной ошибки,
  предотвращающее «перераскрутку» регулятора при длительном насыщении.
* **Лимит скорости выхода** — ограничивает скорость изменения управляющего
  воздействия, что полезно для мягкого старта приводов.

Каждый шаг вычисления сопровождается подробными логами для удобной
отладки и мониторинга.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Tuple

logger = logging.getLogger(__name__)


def _clamp(value: float, limits: Tuple[float, float]) -> float:
    """Просто ограничивает ``value`` диапазоном ``limits``."""
    low, high = limits
    if low is not None and value < low:
        return low
    if high is not None and value > high:
        return high
    return value


@dataclass
class PID:
    """Простейшая реализация PID-регулятора.

    Параметры задаются при создании экземпляра. Метод :meth:`update`
    вычисляет новое управляющее воздействие. Объект можно использовать
    многократно, вызывая `update` для каждого очередного измерения.

    Attributes
    ----------
    kp, ki, kd: коэффициенты П, И и Д соответственно.
    output_limits: кортеж ``(min, max)`` для насыщения выхода.
    integral_limit: абсолютное значение, ограничивающее интеграл ошибки.
    max_output_rate: максимальная скорость изменения выхода (ед/с).
    """

    kp: float
    ki: float
    kd: float
    output_limits: Tuple[float | None, float | None] = (None, None)
    integral_limit: float | None = None
    max_output_rate: float | None = None

    def __post_init__(self) -> None:
        self._integral = 0.0
        self._prev_error: float | None = None
        self._last_output = 0.0
        logger.debug("PID инициализирован: %s", self)

    def reset(self) -> None:
        """Сбрасывает внутреннее состояние регулятора."""
        logger.debug("Сброс состояния PID")
        self._integral = 0.0
        self._prev_error = None
        self._last_output = 0.0

    def update(self, setpoint: float, measurement: float, dt: float) -> float:
        """Вычисляет управляющее воздействие.

        Parameters
        ----------
        setpoint: желаемое значение процесса.
        measurement: текущее измеренное значение.
        dt: шаг интегрирования в секундах.
        """

        error = setpoint - measurement
        logger.debug(
            "PID step: setpoint=%.3f measurement=%.3f error=%.3f dt=%.3f",
            setpoint,
            measurement,
            error,
            dt,
        )

        # Пропорциональная составляющая
        p = self.kp * error

        # Интегральная составляющая с антивиндапом
        self._integral += error * dt
        if self.integral_limit is not None:
            before = self._integral
            self._integral = _clamp(
                self._integral, (-self.integral_limit, self.integral_limit)
            )
            if self._integral != before:
                logger.debug(
                    "Integral clamped: %.3f -> %.3f", before, self._integral
                )
        i = self.ki * self._integral

        # Дифференциальная составляющая
        if self._prev_error is None:
            d = 0.0
        else:
            d = (error - self._prev_error) / dt
        d_term = self.kd * d
        self._prev_error = error

        output = p + i + d_term
        logger.debug(
            "Components: P=%.3f I=%.3f D=%.3f -> raw=%.3f",
            p,
            i,
            d_term,
            output,
        )

        # Ограничение по насыщению
        before = output
        output = _clamp(output, self.output_limits)
        if output != before:
            logger.debug("Output clamped: %.3f -> %.3f", before, output)

        # Ограничение скорости изменения выхода
        if self.max_output_rate is not None:
            max_delta = self.max_output_rate * dt
            delta = output - self._last_output
            if delta > max_delta:
                logger.debug(
                    "Rate limited: delta=%.3f > %.3f", delta, max_delta
                )
                output = self._last_output + max_delta
            elif delta < -max_delta:
                logger.debug(
                    "Rate limited: delta=%.3f < -%.3f", delta, max_delta
                )
                output = self._last_output - max_delta

        logger.debug("Output: %.3f", output)
        self._last_output = output
        return output
