"""Краткосрочный контекст: кольцевой буфер последних событий.

Модуль предоставляет простой API для хранения ограниченного количества
последних реплик или событий.  Используется ``collections.deque`` с
фиксированным размером, по достижении которого самые старые элементы
автоматически удаляются.
"""

from __future__ import annotations

from collections import deque
from typing import Any, Iterable

# Максимальный размер буфера
BUFFER_SIZE = 20

# Сам буфер, ограниченный по длине
_buffer: deque[Any] = deque(maxlen=BUFFER_SIZE)


def add(item: Any) -> None:
    """Добавить новый элемент в буфер."""
    _buffer.append(item)


def extend(items: Iterable[Any]) -> None:
    """Добавить несколько элементов в буфер."""
    _buffer.extend(items)


def get_last(n: int | None = None) -> list[Any]:
    """Вернуть последние *n* элементов (или весь буфер, если *n* не указан)."""
    if n is None or n >= len(_buffer):
        return list(_buffer)
    return list(_buffer)[-n:]
