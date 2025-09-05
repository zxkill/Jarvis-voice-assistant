"""Краткосрочный контекст: кольцевой буфер последних событий.

Модуль предоставляет простой API для хранения ограниченного количества
последних реплик или событий.  Используется ``collections.deque`` с
фиксированным размером, по достижении которого самые старые элементы
автоматически удаляются.
"""

from __future__ import annotations

from collections import deque
import logging
from typing import Any, Iterable

# Логгер модуля для детальной отладки
logger = logging.getLogger(__name__)

# Максимальный размер буфера
BUFFER_SIZE = 20

# Сам буфер, ограниченный по длине
_buffer: deque[Any] = deque(maxlen=BUFFER_SIZE)


def add(item: Any) -> None:
    """Добавить новый элемент в буфер."""
    logger.debug("short_term.add: %s", item)
    _buffer.append(item)


def extend(items: Iterable[Any]) -> None:
    """Добавить несколько элементов в буфер."""
    items_list = list(items)
    logger.debug("short_term.extend: %s", items_list)
    _buffer.extend(items_list)


def get_last(n: int | None = None) -> list[Any]:
    """Вернуть последние *n* элементов (или весь буфер, если *n* не указан)."""
    result = list(_buffer) if n is None or n >= len(_buffer) else list(_buffer)[-n:]
    logger.debug("short_term.get_last(%s): %s", n, result)
    return result
