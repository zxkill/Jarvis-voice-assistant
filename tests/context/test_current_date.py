"""Тесты для модуля ``context.current_date``."""

import datetime as dt
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[2]))

from context import current_date


class _FakeDate(dt.date):
    """Фиксированная дата для подмены ``date.today``."""

    @classmethod
    def today(cls) -> "_FakeDate":  # type: ignore[override]
        return cls(2024, 4, 25)


def test_refresh_updates_context(monkeypatch):
    """``refresh`` берёт системную дату и сохраняет её в контекст."""

    monkeypatch.setattr(current_date.dt, "date", _FakeDate)
    assert current_date.refresh() == dt.date(2024, 4, 25)
    # Следующий вызов ``get`` возвращает сохранённую дату без обращения к системе
    monkeypatch.setattr(current_date.dt.date, "today", lambda: dt.date(2000, 1, 1))
    assert current_date.get() == dt.date(2024, 4, 25)
