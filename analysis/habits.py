"""Анализ сессий присутствия пользователя."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import List, Optional

from core.logging_json import configure_logging
from memory.db import get_connection

log = configure_logging("analysis.habits")

AGGREGATES_FILE = Path(__file__).with_name("aggregates.json")


def aggregate_by_hour() -> List[int]:
    """Вернуть суммарную длительность присутствия по часам суток.

    Результат — список из 24 элементов, значения указаны в секундах.
    """
    counts = [0] * 24
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT start_ts, COALESCE(end_ts, ?) AS end_ts FROM presence_sessions",
            (int(time.time()),),
        ).fetchall()
    for row in rows:
        start = int(row["start_ts"])
        end = int(row["end_ts"])
        current = start
        while current < end:
            hour = datetime.fromtimestamp(current).hour
            next_boundary = ((current // 3600) + 1) * 3600
            segment_end = min(end, next_boundary)
            counts[hour] += segment_end - current
            current = segment_end
    return counts


def aggregate_by_weekday() -> List[int]:
    """Вернуть суммарную длительность присутствия по дням недели.

    Результат — список из 7 элементов (``0`` — понедельник), значения в секундах.
    """
    counts = [0] * 7
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT start_ts, COALESCE(end_ts, ?) AS end_ts FROM presence_sessions",
            (int(time.time()),),
        ).fetchall()
    for row in rows:
        start = int(row["start_ts"])
        end = int(row["end_ts"])
        current = start
        while current < end:
            weekday = datetime.fromtimestamp(current).weekday()
            next_boundary = ((current // 86400) + 1) * 86400
            segment_end = min(end, next_boundary)
            counts[weekday] += segment_end - current
            current = segment_end
    return counts


def _save_daily_aggregate(day: date, data: List[int]) -> None:
    """Сохранить агрегаты за *day* в JSON-файл."""
    existing: dict[str, List[int]] = {}
    if AGGREGATES_FILE.exists():
        try:
            existing = json.loads(AGGREGATES_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    existing[day.isoformat()] = data
    AGGREGATES_FILE.write_text(json.dumps(existing, ensure_ascii=False), encoding="utf-8")


def load_last_aggregate() -> Optional[List[int]]:
    """Вернуть агрегаты последнего дня из файла, если они есть."""
    if not AGGREGATES_FILE.exists():
        return None
    try:
        data = json.loads(AGGREGATES_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not data:
        return None
    latest = max(data)
    return data.get(latest)


async def schedule_daily_aggregation() -> None:
    """Планировщик ежедневного расчёта агрегатов."""
    while True:
        start = time.monotonic()
        counts = aggregate_by_hour()
        duration = time.monotonic() - start
        prev = load_last_aggregate()
        _save_daily_aggregate(date.today(), counts)
        log.info("daily aggregation", extra={"ctx": {"duration_sec": round(duration, 3)}})
        if prev:
            for hour, (p, c) in enumerate(zip(prev, counts)):
                diff = c - p
                if abs(diff) >= 3600:
                    log.warning(
                        "deviation",
                        extra={"ctx": {"hour": hour, "diff_sec": int(diff)}},
                    )
        now = datetime.now()
        tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=5, second=0, microsecond=0)
        await asyncio.sleep((tomorrow - now).total_seconds())
