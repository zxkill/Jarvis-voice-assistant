"""Модуль генерации приветствий в зависимости от времени суток.

Предоставляет функции для выбора подходящего приветствия на основе
текущего времени и обработки простых событий.  В модуле активно
используется подробное логирование для удобной отладки и анализа
поведения ассистента.
"""

from __future__ import annotations

import datetime as _dt
import logging
import random
from typing import Callable, Iterable

# Настройка логгера модуля
logger = logging.getLogger(__name__)


def generate_greeting(
    current_time: _dt.datetime | None = None,
    rand_func: Callable[[Iterable[str]], str] | None = None,
) -> str:
    """Возвращает приветствие в зависимости от времени суток.

    Parameters
    ----------
    current_time: datetime.datetime | None
        Текущее время. Если ``None``, используется ``datetime.datetime.now``.
    rand_func: Callable[[Iterable[str]], str] | None
        Функция выбора случайного элемента. По умолчанию ``random.choice``.

    Returns
    -------
    str
        Сгенерированное приветствие на русском языке.

    Raises
    ------
    ValueError
        Если час выходит за диапазон ``0..23``.
    """
    now = current_time or _dt.datetime.now()
    chooser = rand_func or random.choice
    logger.debug("Получено время %s", now)

    hour = now.hour
    if not 0 <= hour <= 23:
        logger.error("Недопустимый час: %s", hour)
        raise ValueError("Час должен быть в диапазоне 0-23")

    # Списки приветствий для разных периодов суток
    if 5 <= hour <= 11:
        variants = ["Доброе утро", "С добрым утром"]
    elif 12 <= hour <= 17:
        variants = ["Добрый день", "Приветствую"]
    elif 18 <= hour <= 22:
        variants = ["Добрый вечер", "Хорошего вечера"]
    else:
        variants = ["Доброй ночи", "Спокойной ночи"]

    greeting = chooser(variants)
    logger.info("Выбрано приветствие: %s", greeting)
    return greeting


def process_event(
    event_name: str,
    current_time: _dt.datetime | None = None,
    rand_func: Callable[[Iterable[str]], str] | None = None,
) -> str:
    """Обрабатывает событие и возвращает приветствие.

    На данный момент поддерживается событие ``"wake"``, которое
    вызывает генерацию приветствия. Для всех остальных событий
    возбуждается ``ValueError``.
    """
    logger.debug("Обработка события: %s", event_name)

    if event_name == "wake":
        return generate_greeting(current_time=current_time, rand_func=rand_func)

    logger.warning("Неизвестное событие: %s", event_name)
    raise ValueError("Неизвестное событие")
