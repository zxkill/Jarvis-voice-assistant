"""Тесты для навыка time_ru."""

import datetime as dt

from skills import time_ru


def test_handle(monkeypatch):
    moment = dt.datetime(2024, 4, 25, 14, 5)
    class FakeDT(dt.datetime):
        @classmethod
        def now(cls):
            return moment
    monkeypatch.setattr(time_ru._dt, "datetime", FakeDT)
    assert time_ru.handle("") == "четырнадцать часов пять минут"

