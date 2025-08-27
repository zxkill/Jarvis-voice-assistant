import datetime as dt
import random

import pytest

from analysis import suggestions
from memory import reader


@pytest.fixture(autouse=True)
def reset_state(monkeypatch):
    """Сбрасывает внутренние флаги перед каждым тестом."""
    suggestions._present = True
    suggestions._last_absent = dt.datetime.now() - dt.timedelta(hours=2)
    suggestions._last_stretch = None
    # Заглушаем фактическую запись подсказок
    monkeypatch.setattr(suggestions, "_emit", lambda text, code: 1)


def test_rejected_suggestion_suppressed(monkeypatch):
    """При частых отказах подсказка почти не показывается."""
    monkeypatch.setattr(
        reader,
        "get_feedback_stats_by_type",
        lambda: {"hourly_stretch": {"accepted": 0, "rejected": 9}},
    )
    # Вероятность ~0.09, случайное число 0.5 — подсказка не должна появиться
    monkeypatch.setattr(random, "random", lambda: 0.5)
    result = suggestions.generate(now=dt.datetime(2024, 1, 1, 10, 0))
    assert result == []


def test_accepted_suggestion_preferred(monkeypatch):
    """Полезные подсказки выдаются чаще."""
    monkeypatch.setattr(
        reader,
        "get_feedback_stats_by_type",
        lambda: {"hourly_stretch": {"accepted": 9, "rejected": 0}},
    )
    # Вероятность ~0.91, случайное число 0.5 — подсказка появится
    monkeypatch.setattr(random, "random", lambda: 0.5)
    result = suggestions.generate(now=dt.datetime(2024, 1, 1, 10, 0))
    assert result == [1]
