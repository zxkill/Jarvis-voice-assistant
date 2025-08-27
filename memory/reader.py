"""Утилиты чтения агрегированной статистики и подсказок."""

from __future__ import annotations

import logging

from .db import get_connection


# Логгер модуля для подробной отладки запросов
logger = logging.getLogger(__name__)


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


def get_suggestion_feedback(suggestion_id: int) -> list[dict]:
    """Получить список отзывов по конкретной подсказке.

    Возвращает упорядоченный по времени список словарей с полями записи.
    Если отзывов нет, возвращается пустой список.
    """

    logger.debug("Запрос отзывов для подсказки id=%s", suggestion_id)
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT id, suggestion_id, response_text, accepted, ts
            FROM suggestion_feedback
            WHERE suggestion_id = ?
            ORDER BY ts
            """,
            (suggestion_id,),
        ).fetchall()
        feedback = [dict(r) for r in rows]
        logger.debug("Найдено отзывов: %d", len(feedback))
        return feedback


def get_feedback_stats() -> dict[str, int]:
    """Вернуть агрегированную статистику по отзывам на подсказки.

    Результат содержит количество принятых и отклонённых подсказок.
    """

    logger.debug("Запрос агрегированной статистики по отзывам")
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT
                SUM(CASE WHEN accepted = 1 THEN 1 ELSE 0 END) AS accepted_count,
                SUM(CASE WHEN accepted = 0 THEN 1 ELSE 0 END) AS rejected_count
            FROM suggestion_feedback
            """,
        ).fetchone()
        accepted_count = int(row["accepted_count"] or 0)
        rejected_count = int(row["rejected_count"] or 0)
        logger.debug(
            "Статистика: принятых=%d, отклонённых=%d",
            accepted_count,
            rejected_count,
        )
        return {"accepted": accepted_count, "rejected": rejected_count}


def get_feedback_stats_by_type() -> dict[str, dict[str, int]]:
    """Вернуть статистику отзывов, сгруппированную по типам подсказок.

    Результат словарь вида ``{reason_code: {"accepted": int, "rejected": int}}``.
    Подсказки без отзывов в результат не попадают.
    """

    logger.debug("Запрос статистики по типам подсказок")
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT s.reason_code AS reason_code,
                   SUM(CASE WHEN f.accepted = 1 THEN 1 ELSE 0 END) AS accepted_count,
                   SUM(CASE WHEN f.accepted = 0 THEN 1 ELSE 0 END) AS rejected_count
            FROM suggestions AS s
            JOIN suggestion_feedback AS f ON s.id = f.suggestion_id
            GROUP BY s.reason_code
            """,
        ).fetchall()

        stats: dict[str, dict[str, int]] = {}
        for row in rows:
            reason_code = str(row["reason_code"])
            stats[reason_code] = {
                "accepted": int(row["accepted_count"] or 0),
                "rejected": int(row["rejected_count"] or 0),
            }

        logger.debug("Статистика по типам: %s", stats)
        return stats
