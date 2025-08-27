"""Тесты профиля движения."""

import logging

from control.motion_profile import MotionProfile

logging.basicConfig(level=logging.DEBUG)


def test_limits_respected():
    """Ускорение и джерк не выходят за пределы."""
    profile = MotionProfile(max_acceleration=2.0, max_jerk=1.0)
    prev_acc = 0.0
    for _ in range(5):
        v, acc, jerk = profile.update(10.0, dt=1.0)
        assert abs(acc) <= 2.0 + 1e-6
        assert abs(jerk) <= 1.0 + 1e-6
        assert abs(acc - prev_acc) <= 1.0 + 1e-6
        prev_acc = acc
