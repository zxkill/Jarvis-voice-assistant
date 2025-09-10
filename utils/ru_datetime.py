# -*- coding: utf-8 -*-
"""Утилиты для работы с русскими датами и склонениями.

Предоставляет функции для преобразования чисел и дат в человеко‑читаемый
вид с правильными склонениями.  Модуль переиспользуется различными
скиллами, чтобы избежать дублирования кода.
"""

from __future__ import annotations

import datetime as _dt
import logging

# Инициализируем отдельный логгер, чтобы сообщения было легко фильтровать
log = logging.getLogger("utils.ru_datetime")

# Названия дней недели в именительном падеже
_DAYS = [
    "понедельник",
    "вторник",
    "среда",
    "четверг",
    "пятница",
    "суббота",
    "воскресенье",
]

# Названия месяцев в родительном падеже
_MONTHS = [
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
]

# Порядковые числительные в среднем роде для чисел 1–31
_DAY_ORDINALS = {
    1: "первое",
    2: "второе",
    3: "третье",
    4: "четвёртое",
    5: "пятое",
    6: "шестое",
    7: "седьмое",
    8: "восьмое",
    9: "девятое",
    10: "десятое",
    11: "одиннадцатое",
    12: "двенадцатое",
    13: "тринадцатое",
    14: "четырнадцатое",
    15: "пятнадцатое",
    16: "шестнадцатое",
    17: "семнадцатое",
    18: "восемнадцатое",
    19: "девятнадцатое",
    20: "двадцатое",
    21: "двадцать первое",
    22: "двадцать второе",
    23: "двадцать третье",
    24: "двадцать четвёртое",
    25: "двадцать пятое",
    26: "двадцать шестое",
    27: "двадцать седьмое",
    28: "двадцать восьмое",
    29: "двадцать девятое",
    30: "тридцатое",
    31: "тридцать первое",
}

# Слова для чисел 0–19. По умолчанию используются формы мужского рода.
_NUM_0_19 = [
    "ноль",
    "один",
    "два",
    "три",
    "четыре",
    "пять",
    "шесть",
    "семь",
    "восемь",
    "девять",
    "десять",
    "одиннадцать",
    "двенадцать",
    "тринадцать",
    "четырнадцать",
    "пятнадцать",
    "шестнадцать",
    "семнадцать",
    "восемнадцать",
    "девятнадцать",
]

# Десятки для чисел 20–59
_TENS = {
    20: "двадцать",
    30: "тридцать",
    40: "сорок",
    50: "пятьдесят",
}

# Словарь для обратного преобразования слов в числа
_NUM_WORDS = {
    "ноль": 0,
    "ноля": 0,
    "один": 1,
    "одна": 1,
    "одну": 1,
    "одной": 1,
    "два": 2,
    "две": 2,
    "три": 3,
    "четыре": 4,
    "пять": 5,
    "шесть": 6,
    "семь": 7,
    "восемь": 8,
    "девять": 9,
    "десять": 10,
    "одиннадцать": 11,
    "двенадцать": 12,
    "тринадцать": 13,
    "четырнадцать": 14,
    "пятнадцать": 15,
    "шестнадцать": 16,
    "семнадцать": 17,
    "восемнадцать": 18,
    "девятнадцать": 19,
    "двадцать": 20,
    "тридцать": 30,
    "сорок": 40,
    "пятьдесят": 50,
    "шестьдесят": 60,
}


def weekday_name(weekday: int) -> str:
    """Вернуть название дня недели в именительном падеже."""

    log.debug("преобразование номера дня недели", extra={"ctx": {"weekday": weekday}})
    return _DAYS[weekday]


def month_genitive(month: int) -> str:
    """Вернуть название месяца в родительном падеже."""

    log.debug("преобразование номера месяца", extra={"ctx": {"month": month}})
    return _MONTHS[month - 1]


def day_ordinal(day: int) -> str:
    """Вернуть порядковое числительное для даты (средний род)."""

    log.debug("преобразование номера дня", extra={"ctx": {"day": day}})
    return _DAY_ORDINALS[day]


def format_date(day: _dt.date) -> str:
    """Вернуть строку вида «двадцать пятое апреля, четверг».

    :param day: дата, которую требуется озвучить
    :return: человеко‑читаемая строка с днём недели
    """

    log.debug("форматирование даты", extra={"ctx": {"day": day.isoformat()}})
    weekday = weekday_name(day.weekday())
    month = month_genitive(day.month)
    day_word = day_ordinal(day.day)
    return f"{day_word} {month}, {weekday}"


def format_date_full(day: _dt.date) -> str:
    """Вернуть строку вида «двадцать пятое апреля 2024 года».

    Используется там, где требуется озвучить полную дату без дня недели.
    """

    log.debug("форматирование полной даты", extra={"ctx": {"day": day.isoformat()}})
    month = month_genitive(day.month)
    day_word = day_ordinal(day.day)
    return f"{day_word} {month} {day.year} года"


def num_to_words(n: int, feminine: bool = False) -> str:
    """Преобразовать число 0–59 в слова.

    :param n: число для преобразования
    :param feminine: использовать ли формы женского рода (``две``, ``одна``)
    """

    log.debug("преобразование числа в слова", extra={"ctx": {"n": n, "feminine": feminine}})
    if 0 <= n < 20:
        word = _NUM_0_19[n]
    elif 20 <= n < 60:
        tens, ones = divmod(n, 10)
        word = _TENS[tens * 10]
        if ones:
            word += f" {_NUM_0_19[ones]}"
    else:
        raise ValueError("n must be between 0 and 59")

    if feminine:
        if word.endswith("один"):
            return word[:-4] + "одна" if word != "один" else "одна"
        if word.endswith("два"):
            return word[:-3] + "две" if word != "два" else "две"
    return word


def hours_decl(h: int) -> str:
    """Подобрать правильную форму слова «час» для числа ``h``."""

    log.debug("склонение слова час", extra={"ctx": {"h": h}})
    if h % 10 == 1 and h != 11:
        return "час"
    if 2 <= h % 10 <= 4 and not 12 <= h <= 14:
        return "часа"
    return "часов"


def minutes_decl(m: int) -> str:
    """Подобрать правильную форму слова «минута» для числа ``m``."""

    log.debug("склонение слова минута", extra={"ctx": {"m": m}})
    if m % 10 == 1 and m != 11:
        return "минута"
    if 2 <= m % 10 <= 4 and not 12 <= m <= 14:
        return "минуты"
    return "минут"


def seconds_decl(s: int) -> str:
    """Подобрать правильную форму слова «секунда» для числа ``s``."""

    log.debug("склонение слова секунда", extra={"ctx": {"s": s}})
    if s % 10 == 1 and s != 11:
        return "секунда"
    if 2 <= s % 10 <= 4 and not 12 <= s <= 14:
        return "секунды"
    return "секунд"


def format_time(now: _dt.datetime) -> str:
    """Вернуть строку вида «пять часов десять минут».

    Минуты пропускаются, если они равны нулю.
    """

    log.debug("форматирование времени", extra={"ctx": {"now": now.isoformat()}})
    h, m = now.hour, now.minute
    h_words = num_to_words(h)
    h_word = hours_decl(h)
    if m == 0:
        return f"{h_words} {h_word} ровно"
    m_words = num_to_words(m, feminine=True)
    m_word = minutes_decl(m)
    return f"{h_words} {h_word} {m_words} {m_word}"


def words_to_number(chunk: str) -> int | None:
    """Конвертировать фразу «двадцать пять» → 25."""

    log.debug("конвертация слов в число", extra={"ctx": {"chunk": chunk}})
    numbers: list[int] = []
    acc: int | None = None
    for w in chunk.lower().split():
        if w.isdigit():
            numbers.append(int(w))
            acc = None
            continue
        val = _NUM_WORDS.get(w)
        if val is None:
            if acc is not None:
                numbers.append(acc)
                acc = None
            continue
        if val < 10 and acc is not None and acc >= 20:
            numbers.append(acc + val)
            acc = None
        elif val % 10 == 0 and val >= 20:
            acc = val
        else:
            numbers.append(val)
            acc = None
    if acc is not None:
        numbers.append(acc)
    return numbers[0] if numbers else None


def to_int(tok: str) -> int | None:
    """Преобразовать отдельное слово или число в ``int``."""

    tok = tok.strip()
    log.debug("преобразование токена в число", extra={"ctx": {"tok": tok}})
    return int(tok) if tok.isdigit() else words_to_number(tok)
