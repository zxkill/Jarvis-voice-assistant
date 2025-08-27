import time
import pytest

import emotion.policy as policy
from emotion.policy import select, PolicyResult


@pytest.fixture(autouse=True)
def reset_policy_state():
    """Сбрасывает внутреннее состояние политики перед каждым тестом."""
    policy._last_icon = None
    policy._last_switch_ts = 0.0


def test_boundary_zones_positive_quadrant():
    """Граница valence=0 должна относиться к позитивной зоне."""
    res = select(0.0, 0.5)
    assert isinstance(res, PolicyResult)
    assert res.icon == "HAPPY"


def test_boundary_zones_negative_valence():
    """Отрицательная валентность при arousal=0 попадает в верхнюю левую зону."""
    res = select(-0.1, 0.0)
    assert res.icon == "ANGRY"


def test_boundary_zones_negative_arousal():
    """Нулевая валентность и отрицательное возбуждение → спокойная зона."""
    res = select(0.0, -0.5)
    assert res.icon == "CALM"


def test_lower_left_zone():
    """Отрицательные valence и arousal должны давать грустную эмоцию."""
    res = select(-0.2, -0.3)
    assert res.icon == "SAD"


def test_icon_throttling(monkeypatch):
    """Иконка не должна меняться чаще минимального интервала."""
    first = select(0.5, 0.5)
    assert first.icon == "HAPPY"
    # Пытаемся сменить иконку сразу же на другую зону
    second = select(-0.5, 0.5)
    assert second.icon == "HAPPY"
    # По прошествии интервала иконка должна смениться
    base = time.monotonic()
    monkeypatch.setattr(time, "monotonic", lambda: base + 10)
    third = select(-0.5, 0.5)
    assert third.icon == "ANGRY"
