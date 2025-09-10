# -*- coding: utf-8 -*-
"""
Скилл «Текущее время» (RU)
==========================

Отвечает на вопросы:
• «Который час?»
• «Сколько времени?» / «Сколько время?»
• «Сейчас времени?» / «Текущее время»

Произносит время словами:
• «двадцать три часа пятнадцать минут»
• «восемнадцать часов одна минута»
• «два часа ровно»
• «один час двадцать пять минут»
• «один час тридцать две минуты»
"""

from __future__ import annotations
import datetime as _dt
from typing import List

import logging

from display import get_driver, DisplayItem
from utils import ru_datetime as _ru_dt

# Фразы‑активаторы для jarvis_skills.py
PATTERNS: List[str] = [
    "который час", "сколько времени", "сколько время", "текущее время",
    "сейчас времени", "сколько сейчас времени",
]

# Интервал автообновления отображения времени на дисплее в секундах
AUTO_UPDATE_INTERVAL = 30

# Инициализируем логгер модуля
log = logging.getLogger("skills.time_ru")


def _format_time_display(now: _dt.datetime) -> str:
    """Формат для вывода на дисплей (дата и время)."""

    return now.strftime("%d-%m %H:%M")

def handle(_: str) -> str:
    """Озвучить текущие часы и минуты с правильными склонениями."""

    now = _dt.datetime.now()
    log.info("озвучиваем текущее время", extra={"ctx": {"now": now.isoformat()}})
    return _ru_dt.format_time(now)

def auto_update():
    """Вызывается планировщиком — обновляем время на дисплее."""
    now = _dt.datetime.now()
    disp_str = _format_time_display(now)
    driver = get_driver()
    log.debug("обновление времени на дисплее", extra={"ctx": {"display": disp_str}})
    driver.draw(DisplayItem(kind="time", payload=disp_str))
