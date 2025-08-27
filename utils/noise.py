"""Простейшие функции генерации шума."""

from __future__ import annotations

import math
import random


def _fade(t: float) -> float:
    """Функция сглаживания Перлина."""
    return t * t * t * (t * (t * 6 - 15) + 10)


def perlin(x: float, seed: int = 0) -> float:
    """Одномерный шум Перлина для моделирования плавного дрейфа.

    Значение находится в интервале ``[-1, 1]`` и зависит от ``x`` и ``seed``.
    Используется для расчёта микродвижений взгляда.
    """
    x0 = math.floor(x)
    x1 = x0 + 1

    def gradient(h: int) -> float:
        rnd = random.Random(seed + h)
        return rnd.uniform(-1, 1)

    g0 = gradient(x0)
    g1 = gradient(x1)
    t = x - x0
    n0 = g0 * (x - x0)
    n1 = g1 * (x - x1)
    fade_t = _fade(t)
    return (1 - fade_t) * n0 + fade_t * n1
