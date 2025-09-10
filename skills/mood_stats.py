"""Скилл для предоставления статистики настроения через Telegram."""

from __future__ import annotations

import logging
from statistics import mean

from memory import db

# Набор фраз, по которым активируется данный скилл.
PATTERNS = [
    "статистика настроения",
    "полная статистика",
    "настроение статистика",
]

# Логгер навыка для удобной отладки и мониторинга.
log = logging.getLogger("skills.mood_stats")


def _format(value: float) -> str:
    """Вспомогательный форматтер для чисел с двумя знаками после запятой."""
    return f"{value:.2f}"


def handle(text: str) -> str:
    """Вернуть агрегированную статистику настроения.

    Функция считывает историю настроения из БД, вычисляет количество
    записей, средние значения валентности и возбуждения, а также
    возвращает последние зафиксированные координаты. Ответ формируется
    на русском языке, чтобы его было удобно читать в Telegram.
    """

    log.info("запрос полной статистики", extra={"ctx": {"text": text}})
    records = db.get_mood_history(limit=1000)
    if not records:
        log.warning("история настроения пуста")
        return "История настроения пуста"

    valences = [r["valence"] for r in records]
    arousals = [r["arousal"] for r in records]

    avg_val = mean(valences)
    avg_ar = mean(arousals)
    last = records[0]

    summary = (
        "Всего записей: {count}; средняя валентность {val}; "
        "среднее возбуждение {ar}; последнее состояние {last_val}/{last_ar}"
    ).format(
        count=len(records),
        val=_format(avg_val),
        ar=_format(avg_ar),
        last_val=_format(last["valence"]),
        last_ar=_format(last["arousal"]),
    )
    log.debug("готова статистика", extra={"ctx": {"summary": summary}})
    return summary
