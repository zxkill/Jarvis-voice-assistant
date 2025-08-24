"""Скилл: рассказывает о праздниках в России.

Использует API https://date.nager.at для получения списка официальных
праздников.  При ошибках сеть/формата возвращается дружелюбное
сообщение.
"""

from __future__ import annotations

import datetime as _dt
from typing import List

import requests

PATTERNS = [
    "какой сегодня праздник",
    "какие праздники сегодня",
    "какой завтра праздник",
]


def _get_holidays(year: int) -> List[dict]:
    url = f"https://date.nager.at/api/v3/PublicHolidays/{year}/RU"
    resp = requests.get(url, timeout=10)
    resp.raise_for_status()
    return resp.json()


def handle(text: str) -> str:
    day = _dt.date.today()
    if "завтра" in text.lower():
        day += _dt.timedelta(days=1)
    try:
        holidays = _get_holidays(day.year)
        for h in holidays:
            if h.get("date") == day.isoformat():
                return f"{day.strftime('%d %B %Y')} — {h.get('localName')}"
        return "Сегодня официальных праздников нет"
    except Exception:
        return "Не удалось получить информацию о праздниках"
