import json

import pytest

from context import long_term
from memory import db as memory_db


def test_get_events_by_label_handles_invalid_records(tmp_path, monkeypatch):
    """Функция должна игнорировать повреждённые или неожиданные записи."""
    # Перенаправляем базу данных в временный каталог,
    # чтобы тест не влиял на реальные данные.
    monkeypatch.setattr(memory_db, "DB_PATH", tmp_path / "memory.sqlite3")

    # Добавляем корректное событие через публичный API
    long_term.add_daily_event("нормальное событие", ["tag"])

    # Вручную записываем две некорректные записи: поломанный JSON и число
    with memory_db.get_connection() as conn:
        conn.execute(
            "INSERT INTO context_items (key, value, ts) VALUES (?, ?, ?)",
            ("broken", "{", 0),  # невалидный JSON
        )
        conn.execute(
            "INSERT INTO context_items (key, value, ts) VALUES (?, ?, ?)",
            ("number", "42", 0),  # валидный JSON, но не словарь
        )
        conn.commit()

    # Функция должна вернуть только корректное событие и не упасть
    events = long_term.get_events_by_label("tag")
    assert events == ["нормальное событие"]
