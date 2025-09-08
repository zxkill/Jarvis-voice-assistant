"""Функции для записи событий, сессий и подсказок."""

from __future__ import annotations

import json
import time
from typing import Any
from enum import Enum
import logging
import re

from .db import get_connection, encrypt
from .long_memory import store_event as _store_episodic_event


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
    # Сериализуем и шифруем полезную нагрузку события
    data = json.dumps(payload, default=_json_default) if payload is not None else None
    enc_data = encrypt(data) if data is not None else None
    logger.debug("Шифруем payload события: %s", bool(enc_data))
    with get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO events (ts, event_type, payload) VALUES (?, ?, ?)",
            (ts, event_type, enc_data),
        )
        event_id = int(cur.lastrowid)

    # Пытаемся сохранить событие и в долговременной памяти
    try:
        text = f"{event_type}: {payload}" if payload else event_type
        _store_episodic_event(text)
    except Exception:
        # Логируем, но не мешаем основному процессу записи события
        logger.exception("Не удалось сохранить событие в эпизодической памяти")

    return event_id


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


def _fingerprint(text: str) -> str:
    """Построить нормализованный отпечаток текста подсказки.

    Для борьбы с дубликатами убираем пунктуацию, приводим к нижнему
    регистру и заменяем типовые фразы вроде «с днём рождения» на
    единый маркер. Это позволяет отсеивать подсказки с одинаковым
    смыслом, даже если формулировка немного отличается.
    """

    text = text.lower()
    replacements = {
        r"с\s+дн[её]м\s+рожд\w*": "birthday",
        r"поздравляю": "",
    }
    for pattern, repl in replacements.items():
        text = re.sub(pattern, repl, text)
    text = re.sub(r"[^a-zа-я0-9]+", " ", text)
    return text.strip()


def add_suggestion(text: str, reason_code: str | None = None) -> int | None:
    """Добавляет подсказку в очередь и возвращает её ID.

    В актуальной версии уникальность подсказок контролируется по полю
    ``reason_code``. Если подсказка с таким кодом уже была отправлена в
    течение последних суток, новая запись не создаётся. Анализ текста
    больше не выполняется, что ускоряет проверку.

    :param text: текст подсказки
    :param reason_code: уникальный код события, порождающего подсказку
    :return: идентификатор созданной записи или ``None`` при дубликате
    """

    ts = int(time.time())
    fp = _fingerprint(text)
    logger.debug(
        "Добавляем подсказку: text=%r reason_code=%r fp=%s", text, reason_code, fp
    )
    with get_connection() as conn:
        if reason_code:
            # Проверяем, не встречался ли этот код события за последние сутки
            row = conn.execute(
                "SELECT id FROM suggestions WHERE reason_code = ? AND ts > ?",
                (reason_code, ts - 24 * 3600),
            ).fetchone()
            if row:
                logger.info(
                    "Повтор события, подсказка не сохраняется",
                    extra={"ctx": {"reason_code": reason_code, "id": row["id"]}},
                )
                return None

        cur = conn.execute(
            "INSERT INTO suggestions (text, ts, reason_code, fingerprint) VALUES (?, ?, ?, ?)",
            (text, ts, reason_code, fp),
        )
        suggestion_id = int(cur.lastrowid)
        logger.debug("Подсказка сохранена с id=%s", suggestion_id)
        return suggestion_id


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
