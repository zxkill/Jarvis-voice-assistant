import datetime as dt
import random

import pytest

from analysis import suggestions


@pytest.fixture(autouse=True)
def reset_state(monkeypatch):
    """Подготавливает исходные условия перед каждым тестом."""
    suggestions._present = True
    # Пользователь не отсутствовал несколько часов
    suggestions._last_absent = dt.datetime(2024, 1, 1, 7, 0)
    suggestions._last_stretch = None
    suggestions._last_water = None
    suggestions._last_eye_break = None
    suggestions._last_goals_date = None
    # Заглушаем запись подсказок и рандомные выборки
    emitted = []

    def fake_emit(text: str, code: str) -> int:
        emitted.append((text, code))
        return 1

    monkeypatch.setattr(suggestions, "_emit", fake_emit)
    monkeypatch.setattr(random, "choice", lambda seq: seq[0])
    monkeypatch.setattr(random, "random", lambda: 0.0)
    monkeypatch.setattr(suggestions, "get_feedback_stats_by_type", lambda: {})
    yield emitted


def test_hydration_suggestion(reset_state):
    """Напоминание о воде создаётся при соблюдении условий."""
    emitted = reset_state
    suggestions._last_absent = dt.datetime(2024, 1, 1, 9, 50)
    suggestions._last_stretch = dt.datetime(2024, 1, 1, 9, 50)
    suggestions._last_eye_break = dt.datetime(2024, 1, 1, 9, 50)
    ids = suggestions.generate(now=dt.datetime(2024, 1, 1, 10, 0))
    assert ids == [1]
    assert emitted[0][1] == "hydration"
    assert emitted[0][0] in suggestions.SUGGESTION_TEMPLATES["hydration"]["questions"]


def test_eye_break_suggestion(reset_state):
    """Напоминание о перерыве для глаз запускается при длительном присутствии."""
    emitted = reset_state
    suggestions._last_absent = dt.datetime(2024, 1, 1, 9, 20)
    suggestions._last_water = dt.datetime(2024, 1, 1, 9, 30)
    ids = suggestions.generate(now=dt.datetime(2024, 1, 1, 10, 0))
    assert ids == [1]
    assert emitted[0][1] == "eye_break"


def test_daily_goals_once_a_day(reset_state):
    """Напоминание о целях приходит один раз в день."""
    emitted = reset_state
    suggestions._last_absent = dt.datetime(2024, 1, 1, 8, 50)
    suggestions._last_water = dt.datetime(2024, 1, 1, 8, 50)
    suggestions._last_stretch = dt.datetime(2024, 1, 1, 8, 50)
    suggestions._last_eye_break = dt.datetime(2024, 1, 1, 8, 50)
    ids = suggestions.generate(now=dt.datetime(2024, 1, 1, 9, 0))
    assert ids == [1]
    assert emitted[0][1] == "daily_goals"
    # Повторный вызов в тот же день не должен ничего создать
    ids = suggestions.generate(now=dt.datetime(2024, 1, 1, 9, 1))
    assert ids == []


def test_hydration_adaptive(monkeypatch, reset_state):
    """Частый отказ снижает вероятность подсказки, согласие увеличивает."""
    emitted = reset_state
    # Сначала модель считает, что подсказки бесполезны
    monkeypatch.setattr(
        suggestions,
        "get_feedback_stats_by_type",
        lambda: {"hydration": {"accepted": 0, "rejected": 9}},
    )
    monkeypatch.setattr(random, "random", lambda: 0.5)
    ids = suggestions.generate(now=dt.datetime(2024, 1, 1, 10, 0))
    assert ids == []

    # Теперь большинство откликов положительные
    monkeypatch.setattr(
        suggestions,
        "get_feedback_stats_by_type",
        lambda: {"hydration": {"accepted": 9, "rejected": 0}},
    )
    ids = suggestions.generate(now=dt.datetime(2024, 1, 1, 12, 0))
    assert ids == [1]
    assert emitted[0][1] == "hydration"
