import datetime as dt
import sys
from pathlib import Path

# Добавляем корень репозитория в sys.path для корректного импорта
sys.path.append(str(Path(__file__).resolve().parents[2]))

import skills.holiday_ru as holiday_ru  # noqa: E402


def test_is_day_off_holiday(monkeypatch):
    holidays = [{"date": "2024-01-01", "localName": "Новый год"}]
    monkeypatch.setattr(holiday_ru, "_get_holidays", lambda year: holidays)
    flag, name = holiday_ru.is_day_off(dt.date(2024, 1, 1))
    assert flag and "Новый год" in name


def test_is_day_off_weekend(monkeypatch):
    monkeypatch.setattr(holiday_ru, "_get_holidays", lambda year: [])
    flag, name = holiday_ru.is_day_off(dt.date(2024, 7, 6))  # суббота
    assert flag and name == "выходной"


def test_is_day_off_workday(monkeypatch):
    monkeypatch.setattr(holiday_ru, "_get_holidays", lambda year: [])
    flag, name = holiday_ru.is_day_off(dt.date(2024, 7, 3))  # среда
    assert not flag and name == ""


def test_get_it_holiday():
    year = 2024
    assert holiday_ru.get_it_holiday(dt.date(year, 3, 31)) == "День резервного копирования"
    assert holiday_ru.get_it_holiday(dt.date(year, 4, 4)) == "День веб-мастера"
    assert holiday_ru.get_it_holiday(dt.date(year, 9, 9)) == "День тестировщика"
    assert (
        holiday_ru.get_it_holiday(holiday_ru._day_of_programmer(year))
        == "День программиста"
    )
    assert (
        holiday_ru.get_it_holiday(holiday_ru._sysadmin_day(year))
        == "День системного администратора"
    )
    assert holiday_ru.get_it_holiday(dt.date(year, 1, 2)) == ""


def test_handle_it_holiday(monkeypatch):
    monkeypatch.setattr(holiday_ru, "_get_holidays", lambda year: [])

    class FixedDate(dt.date):
        @classmethod
        def today(cls):
            return cls(2024, 9, 9)

    monkeypatch.setattr(holiday_ru._dt, "date", FixedDate)
    assert (
        holiday_ru.handle("какой сегодня праздник")
        == "девятое сентября, понедельник — День тестировщика"
    )
