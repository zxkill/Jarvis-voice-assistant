"""Скилл, сообщающий активность по часам."""

from __future__ import annotations

from typing import List
import logging

from utils import ru_datetime as _ru_dt

from analysis.habits import aggregate_by_hour, load_last_aggregate

PATTERNS = [
    "какая у меня активность по часам",
    "активность по часам",
    "какая активность по часам",
]

# Логгер навыка
log = logging.getLogger("skills.activity_by_hour")


def _format_counts(counts: List[int]) -> str:
    """Преобразовать статистику в текст с правильными склонениями."""

    parts = []
    for hour, sec in enumerate(counts):
        minutes = sec // 60
        if minutes:
            word = _ru_dt.minutes_decl(minutes)
            parts.append(f"{hour:02d}:00 — {minutes} {word}")
    return "; ".join(parts)


def handle(text: str) -> str:
    """Вернуть статистику активности за сутки по часам."""

    log.info("запрос статистики", extra={"ctx": {"text": text}})
    counts = load_last_aggregate() or aggregate_by_hour()
    if not any(counts):
        return "Нет данных об активности"
    return _format_counts(counts)
