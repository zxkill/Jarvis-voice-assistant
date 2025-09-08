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
