"""Генераторы случайных величин."""

from __future__ import annotations

import random
from typing import Optional


def lognormal(mu: float, sigma: float, seed: Optional[int] = None, rng: Optional[random.Random] = None) -> float:
    """Вернуть число из логнормального распределения.

    Параметры ``mu`` и ``sigma`` соответствуют параметрам логнормального
    распределения. Для воспроизводимости можно указать ``seed`` или
    передать собственный экземпляр ``random.Random`` через ``rng``.
    """
    if rng is None:
        rng = random.Random(seed)
    return rng.lognormvariate(mu, sigma)
