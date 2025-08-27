import random

import pytest

from behavior.nodes.actions import blink, micro_saccade


def test_blink_schedule_reproducible():
    """Проверить корректность расписания морганий."""
    gen = blink(start=0.0, mu=0.5, sigma=0.1, seed=7)
    times = [next(gen) for _ in range(3)]

    rng = random.Random(7)
    expected = []
    t = 0.0
    for _ in range(3):
        t += rng.lognormvariate(0.5, 0.1)
        expected.append(t)

    assert times == pytest.approx(expected)


def test_micro_saccade_amplitude_bounds():
    """Амплитуда микросаккады не выходит за заданные пределы."""
    amp = 0.2
    for t in [0.0, 0.3, 1.1, 2.7]:
        angle = micro_saccade(t, amplitude=amp, seed=42)
        assert -amp <= angle <= amp
