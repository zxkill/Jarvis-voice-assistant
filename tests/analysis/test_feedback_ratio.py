"""Тесты функции расчёта долей принятых и отклонённых подсказок."""

from analysis import proactivity


def test_feedback_acceptance_ratio(monkeypatch):
    """Функция корректно обрабатывает наличие и отсутствие статистики."""
    monkeypatch.setattr(proactivity, "get_feedback_stats", lambda: {"accepted": 3, "rejected": 1})
    assert proactivity.feedback_acceptance_ratio() == {"accepted": 0.75, "rejected": 0.25}
    # При отсутствии данных возвращаются нули
    monkeypatch.setattr(proactivity, "get_feedback_stats", lambda: {})
    assert proactivity.feedback_acceptance_ratio() == {"accepted": 0.0, "rejected": 0.0}
