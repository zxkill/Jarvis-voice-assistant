"""Генерация паттернов сканирования для режима IdleScan.

Модуль предоставляет функции для формирования синусоидальных и
треугольных траекторий с добавлением гауссовского шума. Эти паттерны
могут использоваться, например, для "блуждающего" взгляда камеры при
отсутствии пользователя.
"""

from __future__ import annotations

import logging
import math
import random
from typing import List

logger = logging.getLogger(__name__)


def idle_scan(
    mode: str,
    length: int,
    *,
    amplitude: float = 1.0,
    frequency: float = 1.0,
    noise_std: float = 0.0,
) -> List[float]:
    """Генерирует список точек для режима IdleScan.

    Parameters
    ----------
    mode: ``"sine"`` или ``"triangle"``.
    length: длина последовательности.
    amplitude: амплитуда сигнала.
    frequency: количество полных циклов на ``length``.
    noise_std: стандартное отклонение аддитивного гауссовского шума.
    """

    logger.debug(
        "IdleScan: mode=%s length=%d amp=%.3f freq=%.3f noise=%.3f",
        mode,
        length,
        amplitude,
        frequency,
        noise_std,
    )

    if mode == "sine":
        pattern = [
            amplitude * math.sin(2 * math.pi * frequency * t / length)
            for t in range(length)
        ]
    elif mode == "triangle":
        pattern = []
        period = length / frequency
        for t in range(length):
            phase = (t % period) / period
            # Треугольная волна: сначала вверх, потом вниз
            if phase < 0.25:
                value = 4 * phase
            elif phase < 0.75:
                value = 2 - 4 * phase
            else:
                value = -4 + 4 * phase
            pattern.append(amplitude * value)
    else:
        raise ValueError("mode must be 'sine' or 'triangle'")

    if noise_std > 0:
        pattern = [p + random.gauss(0, noise_std) for p in pattern]
        logger.debug("Noise applied with std=%.3f", noise_std)

    logger.debug(
        "IdleScan generated %d points: min=%.3f max=%.3f", 
        len(pattern),
        min(pattern) if pattern else 0.0,
        max(pattern) if pattern else 0.0,
    )
    return pattern
