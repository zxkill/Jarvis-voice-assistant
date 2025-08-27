"""Тесты для PID-регулятора."""

import logging

import pytest

from control.pid import PID

logging.basicConfig(level=logging.DEBUG)


def test_rate_limit():
    """Выход не должен превышать заданную скорость изменения."""
    pid = PID(1.0, 0.0, 0.0, max_output_rate=1.0)
    out1 = pid.update(10.0, 0.0, dt=1.0)
    out2 = pid.update(10.0, 0.0, dt=1.0)
    assert out1 == pytest.approx(1.0)
    assert out2 == pytest.approx(2.0)


def test_anti_windup():
    """Интеграл ошибки ограничивается и не приводит к виндапу."""
    pid = PID(
        0.0,
        1.0,
        0.0,
        output_limits=(-1.0, 1.0),
        integral_limit=0.5,
    )
    for _ in range(5):
        pid.update(10.0, 0.0, dt=1.0)
    assert pid._integral == pytest.approx(0.5)


def test_step_response():
    """PID должен стабилизировать систему на заданном значении."""
    pid = PID(0.8, 0.2, 0.0)
    measurement = 0.0
    for _ in range(20):
        control = pid.update(1.0, measurement, dt=0.1)
        # имитируем простую систему первой степени: интегрируем управление
        measurement += control * 0.1
    assert measurement == pytest.approx(1.0, abs=0.1)
