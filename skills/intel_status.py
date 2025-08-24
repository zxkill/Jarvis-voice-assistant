"""Скилл для отчёта о памяти и добавления заметок в долгосрочный контекст."""

from __future__ import annotations

import json
from datetime import datetime
from typing import List

from context.long_term import add_daily_event
from memory.db import get_connection

PATTERNS = ["что ты запомнил", "запомни:"]


def _format_ts(ts: int) -> str:
    """Преобразовать метку времени в строку."""
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def _get_last_presence() -> str | None:
    """Вернуть описание последней сессии присутствия."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT start_ts, end_ts FROM presence_sessions ORDER BY id DESC LIMIT 1"
        ).fetchone()
    if row is None:
        return None
    start = _format_ts(int(row["start_ts"]))
    end_ts = row["end_ts"]
    if end_ts is None:
        return f"началась {start}, ещё идёт"
    end = _format_ts(int(end_ts))
    return f"{start} – {end}"


def _get_last_context_items(limit: int = 3) -> List[str]:
    """Вернуть последние *limit* записей контекста."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT value FROM context_items ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()
    items: List[str] = []
    for row in rows:
        val = row["value"]
        try:
            data = json.loads(val)
            text = str(data.get("text", ""))
        except Exception:
            text = str(val)
        if text:
            items.append(text)
    items.reverse()  # хронологический порядок
    return items


def handle(text: str) -> str:
    low = text.lower().strip()
    if low.startswith("запомни:"):
        note = text.split(":", 1)[1].strip()
        if not note:
            return "Что запомнить?"
        add_daily_event(note, ["note"])
        return "Запомнил"
    if "что ты запомнил" in low:
        presence = _get_last_presence()
        items = _get_last_context_items()
        parts = []
        if presence:
            parts.append(f"последняя сессия присутствия: {presence}")
        if items:
            parts.append("последние записи: " + "; ".join(items))
        if not parts:
            return "Пока ничего не запомнил"
        return ". ".join(parts)
    return ""
