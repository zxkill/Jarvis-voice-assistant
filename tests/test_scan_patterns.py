"""Тесты генерации паттернов IdleScan."""

import logging
import math

import pytest

from control.scan_patterns import idle_scan

logging.basicConfig(level=logging.DEBUG)


def test_sine_pattern_noise():
    """Синусоидальный паттерн с шумом имеет нужную длину и амплитуду."""
    pattern = idle_scan("sine", length=100, amplitude=1.0, frequency=2.0, noise_std=0.1)
    assert len(pattern) == 100
    approx_amp = max(pattern) - min(pattern)
    assert 1.5 < approx_amp < 2.5  # амплитуда с учётом шума


def test_triangle_pattern():
    """Треугольный паттерн без шума детерминирован."""
    pattern = idle_scan("triangle", length=4, amplitude=1.0, frequency=1.0)
    assert pattern == pytest.approx([0.0, 1.0, 0.0, -1.0])
