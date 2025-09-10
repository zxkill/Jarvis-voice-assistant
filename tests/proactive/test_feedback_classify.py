"""Проверка эвристической классификации ответов на подсказки."""

from proactive.engine import classify_feedback


def test_classify_feedback_positive_negative():
    """Положительные ключевые слова распознаются как принятие."""
    assert classify_feedback("пей воду", "да")[0] is True
    assert classify_feedback("пей воду", "ок")[0] is True
    assert classify_feedback("пей воду", "хорошо")[0] is True
    assert classify_feedback("пей воду", "нет")[0] is False
    assert classify_feedback("пей воду", "подумаю")[0] is False
    # Функция всегда возвращает пустой ответ для унификации интерфейса
    assert classify_feedback("пей воду", "да")[1] == ""
