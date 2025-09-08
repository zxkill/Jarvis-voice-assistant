import datetime as dt

from memory import events


def test_days_until_future_date():
    """Сколько дней осталось до события в текущем году."""
    assert events.days_until(dt.date(1988, 5, 7), today=dt.date(2024, 5, 5)) == 2


def test_days_until_next_year():
    """Если дата уже прошла, рассчитывается следующая годовщина."""
    assert events.days_until(dt.date(1988, 5, 7), today=dt.date(2024, 5, 8)) == 364
