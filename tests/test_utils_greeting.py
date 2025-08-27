"""Тесты для модуля `utils.greeting`.

Проверяются как позитивные сценарии генерации приветствия,
так и негативные случаи с некорректными параметрами и событиями.
Используются фикстуры для имитации времени и случайности, что
обеспечивает детерминированность тестов и близкое к 100% покрытие.
"""

import datetime as _dt
import types

import pytest

from utils import greeting


@pytest.fixture
def time_factory():
    """Фабрика времени для удобного создания фиксированных дат."""

    def _factory(hour: int) -> _dt.datetime:
        return _dt.datetime(2024, 1, 1, hour, 0, 0)

    return _factory


@pytest.fixture
def fake_random_choice():
    """Фикстура, возвращающая детерминированный вариант из списка."""

    def _chooser(seq):
        return list(seq)[0]

    return _chooser


@pytest.mark.parametrize(
    "hour,expected",
    [
        (6, "Доброе утро"),
        (13, "Добрый день"),
        (20, "Добрый вечер"),
        (2, "Доброй ночи"),
    ],
)
def test_generate_greeting_positive(hour, expected, time_factory, fake_random_choice):
    """Позитивные сценарии генерации приветствий."""
    now = time_factory(hour)
    assert greeting.generate_greeting(now, fake_random_choice) == expected


def test_generate_greeting_invalid_hour(time_factory):
    """Негативный тест: час вне диапазона вызывает ошибку."""
    fake_time = types.SimpleNamespace(hour=25)
    with pytest.raises(ValueError):
        greeting.generate_greeting(fake_time)


def test_process_event_positive(time_factory, fake_random_choice):
    """Событие `wake` обрабатывается и возвращает приветствие."""
    now = time_factory(8)
    assert greeting.process_event("wake", now, fake_random_choice) == "Доброе утро"


def test_process_event_unknown(time_factory):
    """Негативный тест: неизвестное событие приводит к ошибке."""
    with pytest.raises(ValueError):
        greeting.process_event("sleep", time_factory(10))
