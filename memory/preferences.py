"""Модуль для сохранения и загрузки пользовательских предпочтений."""

from __future__ import annotations

# Стандартные библиотеки
import json
import logging
from typing import List

from .long_memory import store_fact
from .db import get_connection

# Логгер для отслеживания операций с предпочтениями
logger = logging.getLogger(__name__)


def save_preference(text: str) -> int:
    """Сохранить *text* как предпочтение пользователя.

    Функция оборачивает :func:`memory.long_memory.store_fact`, добавляя
    метку ``type=preference`` для последующей фильтрации. Возвращается
    идентификатор вставленной записи, что упрощает отладку и тестирование.
    """

    logger.debug("Сохранение предпочтения: %s", text)
    return store_fact(text, meta={"type": "preference"})


def load_preferences() -> List[str]:
    """Вернуть список всех ранее сохранённых предпочтений.

    Извлекаются только записи из таблицы ``semantic_memory``, помеченные
    метаданными ``{"type": "preference"}``. Остальные факты игнорируются.
    """

    logger.debug("Загрузка всех пользовательских предпочтений")
    prefs: List[str] = []
    with get_connection() as conn:
        rows = conn.execute("SELECT text, meta FROM semantic_memory").fetchall()
    for row in rows:
        try:
            meta = json.loads(row["meta"] or "{}")
        except json.JSONDecodeError:
            meta = {}
        if meta.get("type") == "preference":
            prefs.append(str(row["text"]))
    logger.debug("Найдено %d предпочтений", len(prefs))
    return prefs
