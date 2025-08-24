"""Глобальный подписчик, записывающий события в хранилище."""

from __future__ import annotations

import time
from typing import Dict

from core.events import Event, subscribe_all
from .writer import write_event

# Минимальный интервал между одинаковыми событиями, секунды
THROTTLE_SECONDS = 1.0

# Время последней записи для каждого типа события
_last_ts: Dict[str, float] = {}


def _should_log(kind: str, now: float) -> bool:
    """Проверяет, стоит ли логировать событие *kind* в момент *now*."""
    last = _last_ts.get(kind, 0.0)
    if now - last < THROTTLE_SECONDS:
        return False
    _last_ts[kind] = now
    return True


def _on_event(event: Event) -> None:
    now = time.time()
    if _should_log(event.kind, now):
        write_event(event.kind, event.attrs)


def setup_event_logging() -> None:
    """Подписывает обработчик на все события."""
    subscribe_all(_on_event)
