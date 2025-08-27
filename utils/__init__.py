"""Вспомогательные утилиты для Jarvis.

Модуль предоставляет упрощённый доступ к часто используемым функциям
и классам.  Здесь агрегируются экспортируемые элементы из подпакета
``utils`` для удобства импорта в других частях проекта.
"""

from .distributions import normal, uniform
from .rate_limiter import RateLimiter
from .greeting import generate_greeting, process_event

__all__ = [
    "normal",
    "uniform",
    "RateLimiter",
    "generate_greeting",
    "process_event",
]
