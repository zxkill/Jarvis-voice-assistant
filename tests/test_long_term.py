"""Тесты для модуля долговременного контекста ``context.long_term``."""

import json
import time

import pytest

from context import long_term
from memory import db as memory_db


def test_add_and_get_events_by_label(tmp_path, monkeypatch):
    """Событие должно сохраняться и извлекаться по метке."""
    # Используем временную базу данных, чтобы не влиять на реальные данные
    monkeypatch.setattr(memory_db, "DB_PATH", tmp_path / "memory.sqlite3")

    # Сохраняем событие через публичный API
    event_id = long_term.add_daily_event("тестовое событие", ["tag", "extra"])

    # Проверяем, что запись появилась в таблицах ``episodic_memory`` и
    # ``event_labels``
    with memory_db.get_connection() as conn:
        row = conn.execute(
            "SELECT text, meta FROM episodic_memory WHERE rowid=?",
            (int(event_id),),
        ).fetchone()
        assert row["text"] == "тестовое событие"
        meta = json.loads(row["meta"])
        assert "tag" in meta["labels"]

        labels = conn.execute(
            "SELECT label FROM event_labels WHERE event_id=?",
            (int(event_id),),
        ).fetchall()
        assert {row["label"] for row in labels} == {"tag", "extra"}

        # Проверяем, что индекс по меткам создан
        idx_list = conn.execute("PRAGMA index_list(event_labels)").fetchall()
        assert any(r["name"] == "idx_event_labels_label" for r in idx_list)

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


def test_get_events_by_label_performance(tmp_path, monkeypatch):
    """SQL-фильтрация должна быть быстрее на больших объёмах."""

    monkeypatch.setattr(memory_db, "DB_PATH", tmp_path / "memory.sqlite3")

    # Создаём много событий с разными метками
    for i in range(500):
        long_term.add_daily_event(f"event {i}", ["noise"])
    long_term.add_daily_event("целевое событие", ["target"])

    # Вспомогательная функция повторяет старый подход фильтрации
    def naive(label: str):
        with memory_db.get_connection() as conn:
            rows = conn.execute("SELECT text, meta FROM episodic_memory").fetchall()
        result = []
        for row in rows:
            try:
                meta = json.loads(row["meta"])
            except Exception:
                continue
            if label in meta.get("labels", []):
                result.append(str(row["text"]))
        return result

    start = time.perf_counter()
    new_events = long_term.get_events_by_label("target")
    new_elapsed = time.perf_counter() - start

    start = time.perf_counter()
    old_events = naive("target")
    old_elapsed = time.perf_counter() - start

    assert new_events == ["целевое событие"]
    assert old_events == ["целевое событие"]
    assert new_elapsed <= old_elapsed

