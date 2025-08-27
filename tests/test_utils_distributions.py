"""Тесты для модуля ``utils.distributions``.

Цель — проверить корректность генерации случайных величин и влияние
параметров ``seed`` и внешнего генератора.
"""

from __future__ import annotations

import random

from utils.distributions import normal, uniform


def test_uniform_seed_reproducible() -> None:
    """Равномерное распределение должно зависеть от seed."""
    assert uniform(0, 1, seed=1) == uniform(0, 1, seed=1)


def test_uniform_bounds() -> None:
    """Сгенерированное значение лежит в указанном диапазоне."""
    value = uniform(-1, 1, seed=2)
    assert -1 <= value <= 1


def test_normal_seed_reproducible() -> None:
    """Нормальное распределение повторяемо при одинаковом seed."""
    assert normal(0, 1, seed=1) == normal(0, 1, seed=1)


def test_normal_statistics() -> None:
    """Среднее большого набора значений близко к ожиданию."""
    rng = random.Random(0)
    samples = [normal(0, 1, rng=rng) for _ in range(1000)]
    mean = sum(samples) / len(samples)
    assert abs(mean) < 0.1
