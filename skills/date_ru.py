# -*- coding: utf-8 -*-
"""Скилл «Календарь» (RU)
==========================

Отвечает на вопросы:
• «какой сегодня день?»
• «какое сегодня число?»
• «завтра какой день?»
"""

from __future__ import annotations

import datetime as _dt
import logging
from typing import List

from context import current_date  # общий источник правдивой даты
from utils import ru_datetime as _ru_dt


# Инициализируем отдельный логгер, чтобы сообщения было легко фильтровать
log = logging.getLogger("skills.date_ru")

# Фразы‑активаторы для ``jarvis_skills.py``
PATTERNS: List[str] = [
    "какой сегодня день",
    "какое сегодня число",
    "сегодня какой день",
    "сегодня какое число",
    "какой завтра день",
    "завтра какой день",
    "завтра какое число",
]


def handle(text: str) -> str:
    """Ответить пользователю текущей или завтрашней датой.

    Дата произносится с корректными склонениями чисел и месяцев.
    Если в запросе встречается слово «завтра», берём дату следующего дня,
    иначе озвучиваем сегодняшний.
    """

    log.debug("получен запрос", extra={"ctx": {"text": text}})

    # Берём актуальную дату из контекста и одновременно обновляем её,
    # чтобы другие модули знали правильный день
    day = current_date.refresh()
    if "завтра" in text.lower():
        day += _dt.timedelta(days=1)
        log.info("рассчитываем дату на завтра", extra={"ctx": {"day": day.isoformat()}})
    else:
        log.info("рассчитываем дату на сегодня", extra={"ctx": {"day": day.isoformat()}})

    # Используем общий модуль склонений для формирования ответа
    return _ru_dt.format_date(day)
