"""Долгосрочный контекст, основанный на таблице ``context_items``.

Позволяет добавлять «события дня» с произвольными метками и
извлекать их по одной метке.  Хранение осуществляется в таблице
``context_items`` БД памяти (см. ``memory/db.py``).  В поле ``value``
сохраняется JSON с текстом события и списком меток.
"""

from __future__ import annotations

import json
import time
import logging
from typing import Iterable, List

from memory.db import get_connection

# Логгер для наблюдения за операциями долговременной памяти
logger = logging.getLogger(__name__)


def add_daily_event(text: str, labels: Iterable[str]) -> str:
    """Добавить событие дня и вернуть его ключ."""
    ts = int(time.time())
    key = f"event:{ts}"
    payload = json.dumps({"text": text, "labels": list(labels)})
    with get_connection() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO context_items (key, value, ts) VALUES (?, ?, ?)",
            (key, payload, ts),
        )
    logger.debug("long_term.add_daily_event: %s -> %s", labels, text)
    return key


def get_events_by_label(label: str) -> List[str]:
    """Вернуть список текстов событий, помеченных *label*."""
    with get_connection() as conn:
        rows = conn.execute("SELECT value FROM context_items").fetchall()
    events: List[str] = []
    for row in rows:
        try:
            data = json.loads(row["value"])
        except Exception:
            # Если значение не парсится как JSON, запись повреждена —
            # пропускаем её, чтобы не ломать остальной вывод.
            logger.debug(
                "long_term.get_events_by_label: пропуск повреждённой записи",
                extra={"label": label},
            )
            continue

        # В старых версиях данные могли быть не словарём, например числом.
        # Чтобы не получить AttributeError при .get(), проверяем тип.
        if not isinstance(data, dict):
            logger.debug(
                "long_term.get_events_by_label: неожиданное значение %r",
                data,
                extra={"label": label},
            )
            continue

        # Добавляем текст события, если среди меток присутствует нужная.
        if label in data.get("labels", []):
            events.append(str(data.get("text", "")))
    logger.debug("long_term.get_events_by_label(%s): %s", label, events)
    return events
