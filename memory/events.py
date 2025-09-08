
"""Утилиты для извлечения дат пользовательских событий из памяти.

Модуль ищет в семантической памяти упоминания событий с датами
(например, «день рождения 7 мая 1988 года») и проверяет, насколько
близко наступление события. Реализация универсальна и подходит для
любых фактов пользователя, содержащих дату.
"""
from __future__ import annotations

import datetime as dt
import logging
import re
from typing import Optional

from .db import get_connection

# Логгер модуля для подробной отладки
log = logging.getLogger(__name__)

# Соответствие русских названий месяцев их порядковым номерам
_MONTHS = {
    "января": 1,
    "февраля": 2,
    "марта": 3,
    "апреля": 4,
    "мая": 5,
    "июня": 6,
    "июля": 7,
    "августа": 8,
    "сентября": 9,
    "октября": 10,
    "ноября": 11,
    "декабря": 12,
}

def _today() -> dt.date:
    """Выделено для удобной подмены текущей даты в тестах."""
    return dt.date.today()

def load_event_date(keyword: str) -> Optional[dt.date]:
    """Извлечь из семантической памяти дату события по ключевому слову.

    В таблице ``semantic_memory`` ищется текст, содержащий ``keyword``.
    Ожидается, что в найденной записи присутствует дата в формате
    ``7 мая 1988`` или ``07.05.1988``. Возвращается объект ``date`` либо
    ``None``, если событие не найдено или дата некорректна.
    """
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT text FROM semantic_memory WHERE lower(text) LIKE ?",
            (f"%{keyword.lower()}%",),
        ).fetchall()

    for row in rows:
        text = str(row["text"]).lower()
        # Пытаемся распарсить формат "07.05.1988"
        m = re.search(r"(\d{1,2})[.](\d{1,2})[.](\d{4})", text)
        if m:
            day, month, year = map(int, m.groups())
            try:
                return dt.date(year, month, day)
            except ValueError:
                log.debug("некорректная дата: %s", m.group(0))
                continue
        # Пытаемся распарсить формат "7 мая 1988"
        m = re.search(r"(\d{1,2})\s+(\w+)\s+(\d{4})", text)
        if m:
            day = int(m.group(1))
            month = _MONTHS.get(m.group(2))
            year = int(m.group(3))
            if month:
                try:
                    return dt.date(year, month, day)
                except ValueError:
                    log.debug("некорректная дата: %s", m.group(0))
                    continue
    log.debug("Дата для события %s не найдена", keyword)
    return None

def days_until(date: dt.date, today: dt.date | None = None) -> int:
    """Сколько дней осталось до указанного ежегодного события."""
    today = today or _today()
    upcoming = dt.date(today.year, date.month, date.day)
    if upcoming < today:
        upcoming = dt.date(today.year + 1, date.month, date.day)
    return (upcoming - today).days

def is_event_soon(keyword: str, window: int = 7) -> bool:
    """Проверить, близко ли событие ``keyword`` к текущей дате.

    :param keyword: фраза для поиска записи (например, "день рожд")
    :param window: число дней, в пределах которых событие считается
                   "скоро" (по умолчанию неделя)
    :return: ``True``, если событие наступит в ближайшие ``window`` дней
    """
    date = load_event_date(keyword)
    if not date:
        log.info("нет сведений о событии %s", keyword)
        return False
    delta = days_until(date)
    log.debug("До события '%s' %d дней", keyword, delta)
    return 0 <= delta <= window
