"""Скилл для отчёта о памяти и добавления заметок в долгосрочный контекст."""

from __future__ import annotations

# Стандартные библиотеки
import json
import logging
from datetime import datetime
from typing import List

# Внутренние модули проекта
from context.long_term import add_daily_event, get_events_by_label
from memory.db import get_connection
from memory.preferences import save_preference

# Логгер с пространством имён модуля для удобного поиска сообщений
logger = logging.getLogger(__name__)

PATTERNS = [
    "что ты запомнил",
    "запомни:",
    "запомни",
    "события дня",
    "что запомнил",
]

LABEL = "note"


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
    """Основная точка входа для обработки пользовательской команды."""

    low = text.lower().strip()

    # ─── Режим сохранения заметок или предпочтений ────────────────
    if low.startswith("запомни"):
        # отрезаем ключевое слово и лишние разделители перед содержанием
        note = text[len("запомни") :].lstrip(" ,:")
        if not note:
            return "Что запомнить?"

        note_low = note.lower()
        if note_low.startswith("что"):
            # Пользователь формулирует устойчивое предпочтение
            pref_text = note[3:].lstrip(" ,:")
            logger.debug("Сохранение предпочтения: %s", pref_text)
            save_preference(pref_text)
        else:
            # Обычная заметка дня
            logger.debug("Сохранение заметки: %s", note)
            add_daily_event(note, [LABEL])
        return "Запомнил"

    # ─── Режим отчёта о сохранённых записях ──────────────────────
    if any(p in low for p in ["что ты запомнил", "что запомнил", "события дня"]):
        presence = _get_last_presence()
        items = _get_last_context_items()
        events = get_events_by_label(LABEL)
        parts = []
        if presence:
            parts.append(f"последняя сессия присутствия: {presence}")
        if items:
            parts.append("последние записи: " + "; ".join(items))
        if events:
            parts.append("события дня: " + "; ".join(events))
        if not parts:
            return "Пока ничего не запомнил"
        return ". ".join(parts)

    return ""
