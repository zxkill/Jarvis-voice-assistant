"""Работа с долговременной памятью на основе эмбеддингов.

Здесь реализованы функции сохранения эпизодической и семантической
памяти, а также поиск записей по косинусному сходству.
"""

from __future__ import annotations

# Стандартные библиотеки
import json
import logging
import time
from typing import Dict, List, Tuple

import numpy as np

from .db import get_connection
from .embeddings import get_embedding

# Логгер для отслеживания операций долговременной памяти
logger = logging.getLogger(__name__)


def _serialize_embedding(embedding: List[float]) -> str:
    """Сериализовать вектор в JSON-строку."""
    return json.dumps(embedding)


def _deserialize_embedding(data: str) -> List[float]:
    """Преобразовать JSON-строку обратно в список чисел."""
    return json.loads(data)


def store_event(text: str, meta: Dict[str, str] | None = None) -> int:
    """Сохранить событие в таблице ``episodic_memory``.

    :param text: текстовое описание события
    :param meta: произвольный словарь с метаданными
    :return: идентификатор новой записи
    """
    ts = int(time.time())
    embedding = get_embedding(text)
    meta_json = json.dumps(meta or {})
    logger.debug("Сохраняем эпизодическое событие: %s", text)
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO episodic_memory (ts, text, embedding, meta)
            VALUES (?, ?, ?, ?)
            """,
            (ts, text, _serialize_embedding(embedding), meta_json),
        )
        event_id = int(cur.lastrowid)
        logger.debug("Эпизод сохранён с id=%d", event_id)
        return event_id


def store_fact(text: str, meta: Dict[str, str] | None = None) -> int:
    """Сохранить факт в таблице ``semantic_memory``.

    :param text: содержание факта
    :param meta: дополнительные метаданные
    :return: идентификатор новой записи
    """
    ts = int(time.time())
    embedding = get_embedding(text)
    meta_json = json.dumps(meta or {})
    logger.debug("Сохраняем семантический факт: %s", text)
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO semantic_memory (ts, text, embedding, meta)
            VALUES (?, ?, ?, ?)
            """,
            (ts, text, _serialize_embedding(embedding), meta_json),
        )
        fact_id = int(cur.lastrowid)
        logger.debug("Факт сохранён с id=%d", fact_id)
        return fact_id


def _cosine_similarity(vec1: np.ndarray, vec2: np.ndarray) -> float:
    """Вычислить косинусное сходство двух векторов."""
    norm1 = np.linalg.norm(vec1)
    norm2 = np.linalg.norm(vec2)
    if norm1 == 0 or norm2 == 0:
        return 0.0
    return float(np.dot(vec1, vec2) / (norm1 * norm2))


def retrieve_similar(query: str, *, top_k: int = 5) -> List[Tuple[str, float]]:
    """Найти наиболее похожие записи в обеих таблицах памяти.

    :param query: строка запроса
    :param top_k: максимальное количество результатов
    :return: список кортежей ``(текст, коэффициент_сходства)``
    """
    query_vec = np.array(get_embedding(query))
    logger.debug("Поиск похожих записей для запроса: %s", query)
    results: List[Tuple[str, float]] = []

    with get_connection() as conn:
        for table in ("episodic_memory", "semantic_memory"):
            rows = conn.execute(
                f"SELECT text, embedding FROM {table}").fetchall()
            for row in rows:
                emb = np.array(_deserialize_embedding(row["embedding"]))
                sim = _cosine_similarity(query_vec, emb)
                results.append((str(row["text"]), sim))
                logger.debug(
                    "Сходство с записью %r из %s: %f",
                    row["text"],
                    table,
                    sim,
                )

    # Сортируем по коэффициенту сходства и возвращаем top_k
    results.sort(key=lambda x: x[1], reverse=True)
    trimmed = results[:top_k]
    logger.debug("Найденные записи: %s", trimmed)
    return trimmed
