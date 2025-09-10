"""Тесты для скилла даты."""

import datetime as dt
import sys
from pathlib import Path

# Добавляем корень репозитория в ``sys.path``, чтобы импортировать скилл напрямую
sys.path.append(str(Path(__file__).resolve().parents[2]))

import skills.date_ru as date_ru  # noqa: E402
from context import current_date  # noqa: E402


def _freeze_today(monkeypatch) -> None:
    """Зафиксировать «сегодняшнюю» дату в контексте."""

    def fake_refresh() -> dt.date:
        return dt.date(2024, 4, 25)  # четверг

    monkeypatch.setattr(current_date, "refresh", fake_refresh)


def test_handle_today(monkeypatch):
    """При запросе сегодняшнего дня возвращается корректная дата."""

    _freeze_today(monkeypatch)
    assert date_ru.handle("какой сегодня день") == "двадцать пятое апреля, четверг"


def test_handle_tomorrow(monkeypatch):
    """При запросе завтрашнего дня дата сдвигается на сутки вперёд."""

    _freeze_today(monkeypatch)
    assert date_ru.handle("какой завтра день") == "двадцать шестое апреля, пятница"
