"""Функции для записи событий, сессий и подсказок."""

from __future__ import annotations

import json
import time
from typing import Any
from enum import Enum
import logging

from .db import get_connection


# Настраиваем логгер для данного модуля
logger = logging.getLogger(__name__)


def _json_default(obj: Any) -> Any:
    """Преобразует объекты, которые json не умеет сериализовать."""
    if isinstance(obj, Enum):
        return obj.value
    raise TypeError(f"Object of type {obj.__class__.__name__} is not JSON serializable")


def write_event(event_type: str, payload: dict[str, Any] | None = None) -> int:
    """Сохраняет сырое событие и возвращает его ID."""
    ts = int(time.time())  # текущая метка времени
    data = json.dumps(payload, default=_json_default) if payload is not None else None
    with get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO events (ts, event_type, payload) VALUES (?, ?, ?)",
            (ts, event_type, data),
        )
        return int(cur.lastrowid)


def start_session(user_id: str) -> int:
    """Открывает сессию присутствия пользователя и возвращает её ID."""
    ts = int(time.time())
    with get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO presence_sessions (user_id, start_ts) VALUES (?, ?)",
            (user_id, ts),
        )
        return int(cur.lastrowid)


def end_session(session_id: int) -> None:
    """Завершает сессию, проставляя конечную метку времени."""
    ts = int(time.time())
    with get_connection() as conn:
        conn.execute(
            "UPDATE presence_sessions SET end_ts = ? WHERE id = ?",
            (ts, session_id),
        )


def add_suggestion(text: str) -> int:
    """Добавляет подсказку в очередь и возвращает её ID."""
    ts = int(time.time())
    with get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO suggestions (text, ts) VALUES (?, ?)",
            (text, ts),
        )
        return int(cur.lastrowid)


def add_suggestion_feedback(suggestion_id: int, response_text: str, accepted: bool) -> int:
    """Сохраняет ответ пользователя на подсказку и возвращает ID записи.

    :param suggestion_id: идентификатор подсказки, на которую получен ответ
    :param response_text: текст ответа пользователя
    :param accepted: флаг, была ли подсказка принята (``True``) или отклонена
    :return: идентификатор созданной записи в таблице ``suggestion_feedback``
    """

    # Фиксируем момент добавления записи
    ts = int(time.time())
    logger.debug(
        "Добавляем отзыв: suggestion_id=%s accepted=%s text=%r",
        suggestion_id,
        accepted,
        response_text,
    )
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO suggestion_feedback (suggestion_id, response_text, accepted, ts)
            VALUES (?, ?, ?, ?)
            """,
            (suggestion_id, response_text, int(accepted), ts),
        )
        feedback_id = int(cur.lastrowid)
        logger.debug("Отзыв сохранён с id=%s", feedback_id)
        return feedback_id
