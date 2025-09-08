"""Скилл: рассказывает о праздниках в России.

Использует API https://date.nager.at для получения списка официальных
праздников.  При ошибках сеть/формата возвращается дружелюбное
сообщение.
"""

from __future__ import annotations

import datetime as _dt
import logging
from typing import List, Tuple

import requests

# Настраиваем логгер с отдельным неймспейсом для удобной фильтрации
log = logging.getLogger("skills.holiday_ru")

PATTERNS = [
    "какой сегодня праздник",
    "какие праздники сегодня",
    "какой завтра праздник",
]
def _get_holidays(year: int) -> List[dict]:
    """Получить список официальных праздников на указанный год."""

    url = f"https://date.nager.at/api/v3/PublicHolidays/{year}/RU"
    log.debug("запрос списка праздников", extra={"ctx": {"url": url}})
    resp = requests.get(url, timeout=10)
    resp.raise_for_status()
    return resp.json()


def is_day_off(day: _dt.date | None = None) -> Tuple[bool, str]:
    """Определить, является ли указанная дата официальным праздником или выходным.

    Возвращает кортеж ``(флаг, название)``.  Флаг ``True`` означает, что день
    нерабочий, а ``название`` содержит либо имя праздника, либо строку
    ``"выходной"`` для субботы и воскресенья.  Если дата не указана, берётся
    текущий день.
    """

    day = day or _dt.date.today()
    log.debug("проверка дня", extra={"ctx": {"day": day.isoformat()}})

    # --- Проверка выходного по календарю ---------------------------------
    if day.weekday() >= 5:  # 5 и 6 соответствуют субботе и воскресенью
        log.info("определён выходной", extra={"ctx": {"day": day.isoformat()}})
        return True, "выходной"

    # --- Проверка официальных праздников ---------------------------------
    try:
        holidays = _get_holidays(day.year)
    except Exception as exc:  # pragma: no cover - сеть может быть недоступна
        log.exception("не удалось получить праздники", extra={"ctx": {"err": str(exc)}})
        return False, ""
    for h in holidays:
        if h.get("date") == day.isoformat():
            name = str(h.get("localName", ""))
            log.info(
                "обнаружен официальный праздник",
                extra={"ctx": {"day": day.isoformat(), "name": name}},
            )
            return True, name

    log.debug("рабочий день", extra={"ctx": {"day": day.isoformat()}})
    return False, ""


def handle(text: str) -> str:
    """Ответить пользователю информацией о текущем или завтрашнем празднике."""

    day = _dt.date.today()
    if "завтра" in text.lower():
        day += _dt.timedelta(days=1)
    log.debug("обработка запроса", extra={"ctx": {"day": day.isoformat(), "text": text}})
    try:
        flag, name = is_day_off(day)
        if flag and name != "выходной":
            return f"{day.strftime('%d %B %Y')} — {name}"
        if flag:
            return f"{day.strftime('%d %B %Y')} — выходной день"
        return "Сегодня официальных праздников нет"
    except Exception as exc:  # pragma: no cover - сетевые ошибки маловероятны
        log.exception("не удалось получить информацию", extra={"ctx": {"err": str(exc)}})
        return "Не удалось получить информацию о праздниках"
