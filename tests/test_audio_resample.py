"""Тесты для утилиты ресемплинга аудио."""
from __future__ import annotations

import numpy as np
import pytest

from utils.audio_resample import resample_pcm16


def test_resample_returns_same_array_for_matching_rates() -> None:
    """При одинаковых частотах функция должна вернуть исходный объект."""
    data = np.arange(0, 100, dtype=np.int16)
    result = resample_pcm16(data, 16_000, 16_000)
    assert result is data


def test_resample_changes_length_for_different_rates() -> None:
    """Выборка должна масштабироваться пропорционально целевой частоте."""
    source_rate = 22_050
    target_rate = 16_000
    duration = 0.5
    samples = int(source_rate * duration)
    # Генерируем синусоиду, чтобы убедиться в корректности интерполяции.
    time_axis = np.linspace(0.0, duration, samples, endpoint=False)
    sine = (np.sin(2 * np.pi * 440.0 * time_axis) * 30_000).astype(np.int16)

    resampled = resample_pcm16(sine, source_rate, target_rate)

    assert resampled.dtype == np.int16
    assert pytest.approx(resampled.size / target_rate, rel=1e-3) == duration


def test_resample_raises_error_for_invalid_rates() -> None:
    """Нулевые и отрицательные частоты не допускаются."""
    with pytest.raises(ValueError):
        resample_pcm16(np.ones(10, dtype=np.int16), 0, 16_000)

    with pytest.raises(ValueError):
        resample_pcm16(np.ones(10, dtype=np.int16), 16_000, -1)
