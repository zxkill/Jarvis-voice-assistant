"""Тесты для модуля ``utils.ru_datetime``.

Проверяются преобразования дат и чисел в человеко‑читаемый формат
с корректными склонениями.
"""

import datetime as dt

from utils import ru_datetime as ru_dt


def test_day_ordinal():
    assert ru_dt.day_ordinal(1) == "первое"
    assert ru_dt.day_ordinal(25) == "двадцать пятое"


def test_month_genitive():
    assert ru_dt.month_genitive(1) == "января"
    assert ru_dt.month_genitive(12) == "декабря"


def test_format_date():
    day = dt.date(2024, 4, 25)  # четверг
    assert ru_dt.format_date(day) == "двадцать пятое апреля, четверг"


def test_format_date_full():
    day = dt.date(2024, 4, 25)
    assert ru_dt.format_date_full(day) == "двадцать пятое апреля 2024 года"


def test_num_to_words_and_declensions():
    assert ru_dt.num_to_words(2) == "два"
    assert ru_dt.num_to_words(2, feminine=True) == "две"
    assert ru_dt.hours_decl(5) == "часов"
    assert ru_dt.minutes_decl(1) == "минута"
    assert ru_dt.seconds_decl(22) == "секунды"


def test_format_time():
    moment = dt.datetime(2024, 4, 25, 15, 32)
    assert ru_dt.format_time(moment) == "пятнадцать часов тридцать две минуты"
    moment2 = dt.datetime(2024, 4, 25, 15, 0)
    assert ru_dt.format_time(moment2) == "пятнадцать часов ровно"


def test_words_to_number_and_to_int():
    assert ru_dt.words_to_number("двадцать пять") == 25
    assert ru_dt.to_int("сорок два") == 42
