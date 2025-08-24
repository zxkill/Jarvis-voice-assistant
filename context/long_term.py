"""Долгосрочный контекст, основанный на таблице ``context_items``.

Позволяет добавлять «события дня» с произвольными метками и
извлекать их по одной метке.  Хранение осуществляется в таблице
``context_items`` БД памяти (см. ``memory/db.py``).  В поле ``value``
сохраняется JSON с текстом события и списком меток.
"""

from __future__ import annotations

import json
import time
from typing import Iterable, List

from memory.db import get_connection


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
            continue
        if label in data.get("labels", []):
            events.append(str(data.get("text", "")))
    return events
