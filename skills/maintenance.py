# skills/maintenance.py
"""Служебный скилл для управления памятью и рефлексией.

Позволяет вручную запускать ночную рефлексию, агрегировать данные
и очищать разные виды памяти. Это полезно для отладки и исправления
ошибочных записей без ожидания автоматических процедур.
"""

from __future__ import annotations

import logging

from app import scheduler
from context import daily_memory
from memory import db as memory_db

# Логгер модуля
logger = logging.getLogger(__name__)

# Набор фраз, по которым распознаётся этот скилл
PATTERNS = [
    "очисти дневную память",
    "очисти эпизодическую память",
    "очисти семантическую память",
    "очисти дайджесты",
    "запусти рефлексию",
    "агрегируй данные",
]


def handle(text: str, trace_id: str | None = None) -> str:
    """Обработчик служебных команд.

    В зависимости от содержимого входного текста выполняет нужное действие
    и возвращает строку‑подтверждение для озвучивания пользователю.
    """

    text_low = text.lower()
    logger.debug("maintenance skill received", extra={"trace_id": trace_id, "text": text_low})

    if "очист" in text_low and "днев" in text_low:
        daily_memory.clear()
        logger.info("daily memory cleared", extra={"trace_id": trace_id})
        return "Дневная память очищена."

    if "очист" in text_low and ("эпизод" in text_low or "долгоср" in text_low):
        count = memory_db.clear_episodic_memory()
        logger.info(
            "episodic memory cleared", extra={"trace_id": trace_id, "count": count}
        )
        return "Эпизодическая память очищена."

    if "очист" in text_low and "семан" in text_low:
        count = memory_db.clear_semantic_memory()
        logger.info(
            "semantic memory cleared", extra={"trace_id": trace_id, "count": count}
        )
        return "Семантическая память очищена."

    if "очист" in text_low and "дайджест" in text_low:
        count = memory_db.clear_daily_digest()
        logger.info(
            "daily digests cleared", extra={"trace_id": trace_id, "count": count}
        )
        return "Дневные дайджесты очищены."

    if "рефлекс" in text_low or "агрег" in text_low:
        # Пытаемся вручную запустить ночную рефлексию и перенос
        # накопленных данных в долгосрочную память.  Иногда LLM
        # может вернуть некорректный JSON или произойти другая
        # ошибка, поэтому оборачиваем вызов в ``try`` для устойчивости.
        try:
            scheduler._run_nightly_reflection()
        except Exception:
            # Логируем исключение с уровнем ``exception`` – в лог попадёт
            # полный стектрейс, что поможет при диагностике.
            logger.exception(
                "nightly reflection failed", extra={"trace_id": trace_id}
            )
            return "Не удалось выполнить рефлексию и агрегацию данных."

        logger.info("nightly reflection forced", extra={"trace_id": trace_id})
        return "Рефлексия и агрегация данных выполнены."

    logger.debug("maintenance skill: command not recognized", extra={"trace_id": trace_id})
    return "Не понял служебную команду."
