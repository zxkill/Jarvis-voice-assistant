"""Утилиты чтения агрегированной статистики и подсказок."""

from __future__ import annotations

from .db import get_connection


def get_event_counts(start_ts: int, end_ts: int, bucket_seconds: int = 3600) -> list[tuple[int, int]]:
    """Вернуть количество событий, сгруппированных по окнам времени.

    *bucket_seconds* — размер окна агрегации (по умолчанию час).
    Результат: список пар ``(начало_окна, количество)``.
    """
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT (ts / ?) * ? AS bucket, COUNT(*) AS cnt
            FROM events
            WHERE ts BETWEEN ? AND ?
            GROUP BY bucket
            ORDER BY bucket
            """,
            (bucket_seconds, bucket_seconds, start_ts, end_ts),
        ).fetchall()
        return [(int(r["bucket"]), int(r["cnt"])) for r in rows]


def pop_suggestion() -> str | None:
    """Вернуть самую раннюю необработанную подсказку и отметить её."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT id, text FROM suggestions WHERE processed = 0 ORDER BY ts LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        conn.execute("UPDATE suggestions SET processed = 1 WHERE id = ?", (row["id"],))
        return str(row["text"])
