import datetime as dt

from skills import date_ru


def test_handle_today(monkeypatch):
    """Навык должен возвращать сегодняшнюю дату."""
    monkeypatch.setattr(date_ru.current_date, "refresh", lambda: dt.date(2024, 5, 7))
    assert date_ru.handle("какое сегодня число?") == "7 мая, вторник"


def test_handle_tomorrow(monkeypatch):
    """Навык корректно рассчитывает дату на завтра."""
    monkeypatch.setattr(date_ru.current_date, "refresh", lambda: dt.date(2024, 5, 7))
    assert date_ru.handle("какой завтра день?") == "8 мая, среда"


def test_handle_with_extra_words(monkeypatch):
    """Лишние слова не должны влиять на результат."""
    monkeypatch.setattr(date_ru.current_date, "refresh", lambda: dt.date(2024, 5, 7))
    assert date_ru.handle("так скажи какое сегодня число") == "7 мая, вторник"


def test_handle_ignores_foreign_dates(monkeypatch):
    """Указание сторонних дат не должно сбивать навык."""
    monkeypatch.setattr(date_ru.current_date, "refresh", lambda: dt.date(2024, 5, 7))
    text = "мой др 8 января, а какой сегодня день?"
    assert date_ru.handle(text) == "7 мая, вторник"
