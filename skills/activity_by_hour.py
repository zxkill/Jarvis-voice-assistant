"""Скилл, сообщающий активность по часам."""

from __future__ import annotations

from typing import List

from analysis.habits import aggregate_by_hour, load_last_aggregate

PATTERNS = [
    "какая у меня активность по часам",
    "активность по часам",
    "какая активность по часам",
]


def _format_counts(counts: List[int]) -> str:
    parts = []
    for hour, sec in enumerate(counts):
        minutes = sec // 60
        if minutes:
            parts.append(f"{hour:02d}:00 — {minutes} мин")
    return "; ".join(parts)


def handle(text: str) -> str:
    counts = load_last_aggregate() or aggregate_by_hour()
    if not any(counts):
        return "Нет данных об активности"
    return _format_counts(counts)
