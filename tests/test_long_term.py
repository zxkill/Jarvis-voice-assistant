"""Тесты для модуля долговременного контекста ``context.long_term``."""

import json

import pytest

from context import long_term
from memory import db as memory_db


def test_add_and_get_events_by_label(tmp_path, monkeypatch):
    """Событие должно сохраняться и извлекаться по метке."""
    # Используем временную базу данных, чтобы не влиять на реальные данные
    monkeypatch.setattr(memory_db, "DB_PATH", tmp_path / "memory.sqlite3")

    # Сохраняем событие через публичный API
    event_id = long_term.add_daily_event("тестовое событие", ["tag", "extra"])

    # Проверяем, что запись появилась в таблице ``episodic_memory``
    with memory_db.get_connection() as conn:
        row = conn.execute(
            "SELECT text, meta FROM episodic_memory WHERE rowid=?",
            (int(event_id),),
        ).fetchone()
        assert row["text"] == "тестовое событие"
        meta = json.loads(row["meta"])
        assert "tag" in meta["labels"]

    # Извлечение по метке должно вернуть исходный текст
    events = long_term.get_events_by_label("tag")
    assert events == ["тестовое событие"]


def test_get_events_by_label_handles_invalid_records(tmp_path, monkeypatch):
    """Некачественные записи не должны ломать выборку событий."""
    # Перенаправляем базу данных на временный путь
    monkeypatch.setattr(memory_db, "DB_PATH", tmp_path / "memory.sqlite3")

    # Добавляем корректное событие
    long_term.add_daily_event("нормальное событие", ["tag"])

    # Вставляем две испорченные записи вручную
    with memory_db.get_connection() as conn:
        conn.execute(
            "INSERT INTO episodic_memory (ts, text, embedding, meta) VALUES (0, '', '', ?)",
            ("{",),  # метаданные не являются валидным JSON
        )
        conn.execute(
            "INSERT INTO episodic_memory (ts, text, embedding, meta) VALUES (0, '', '', ?)",
            (json.dumps(42),),  # валидный JSON, но не объект
        )
        conn.commit()

    # Функция должна вернуть только корректное событие и не упасть
    events = long_term.get_events_by_label("tag")
    assert events == ["нормальное событие"]

