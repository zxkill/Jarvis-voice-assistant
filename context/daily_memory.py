"""Дневная память для хранения кратких событий дня.

Модуль предоставляет простой интерфейс для добавления кратких записей,
извлечения всех накопленных событий и полной очистки буфера.  Записи
хранятся в таблице ``context_items`` базы данных памяти в виде JSON,
что обеспечивает совместимость с существующей схемой.

Каждая функция снабжена детальным логированием для удобной отладки.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Dict, List

from memory.db import get_connection

# Создаём модульный логгер
logger = logging.getLogger(__name__)

# Префикс ключей для дневной памяти в таблице ``context_items``
_PREFIX = "daily:"


def add(record: Dict[str, str]) -> str:
    """Добавить новую запись в дневную память.

    :param record: словарь с минимум двумя полями:
        ``label`` — текстовая метка события и
        ``text``  — краткое резюме произошедшего.
    :return: сохранённый ключ, который может использоваться для отладки.
    """
    ts = int(time.time())
    key = f"{_PREFIX}{ts}:{uuid.uuid4().hex}"
    payload = json.dumps(record, ensure_ascii=False)
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO context_items (key, value, ts) VALUES (?, ?, ?)",
            (key, payload, ts),
        )
    logger.debug("daily_memory.add: %s", record)
    return key


def fetch_all() -> List[Dict[str, str]]:
    """Извлечь все записи дневной памяти.

    Возвращает список словарей в порядке добавления.  Повреждённые или
    некорректные записи игнорируются, чтобы не мешать остальной обработке.
    """
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT value FROM context_items WHERE key LIKE ? ORDER BY ts",
            (f"{_PREFIX}%",),
        ).fetchall()
    records: List[Dict[str, str]] = []
    for row in rows:
        try:
            data = json.loads(row["value"])
            if isinstance(data, dict):
                records.append({str(k): str(v) for k, v in data.items()})
            else:
                logger.debug("daily_memory.fetch_all: unexpected type %r", data)
        except Exception:
            logger.exception("daily_memory.fetch_all: broken record skipped")
    logger.debug("daily_memory.fetch_all -> %s", records)
    return records


def clear() -> None:
    """Удалить все записи дневной памяти."""
    with get_connection() as conn:
        conn.execute("DELETE FROM context_items WHERE key LIKE ?", (f"{_PREFIX}%",))
    logger.debug("daily_memory.clear: removed all records")
