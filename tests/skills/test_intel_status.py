import json
import sqlite3
import sys
from pathlib import Path

import pytest

# Добавляем корень репозитория в sys.path, чтобы корректно импортировать пакеты
sys.path.append(str(Path(__file__).resolve().parents[2]))

from skills import intel_status  # noqa: E402


class CallRecorder:
    """Простая обёртка для фиксации вызовов и аргументов."""

    def __init__(self):
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))


@pytest.fixture()
def recorders(monkeypatch):
    """Подмена функций сохранения, чтобы отслеживать их вызовы."""

    pref = CallRecorder()
    note = CallRecorder()
    monkeypatch.setattr(intel_status, "save_preference", pref)
    monkeypatch.setattr(intel_status, "add_daily_event", note)
    return pref, note


def test_handle_saves_preference(recorders):
    """Фраза вида «запомни, что ...» должна сохраняться как предпочтение."""

    pref, note = recorders
    reply = intel_status.handle("запомни, что я не ем хлеб")
    assert reply == "Запомнил"
    assert pref.calls == [(("я не ем хлеб",), {})]
    assert note.calls == []


def test_handle_saves_note(recorders):
    """Обычная фраза после «запомни» должна сохраняться как заметка."""

    pref, note = recorders
    reply = intel_status.handle("запомни купить молоко")
    assert reply == "Запомнил"
    assert pref.calls == []
    assert note.calls == [(("купить молоко", [intel_status.LABEL]), {})]


def test_get_last_context_items_filters_extraneous(monkeypatch, caplog):
    """Функция должна игнорировать записи с чужими префиксами."""

    # Создаём временную БД в памяти и заполняем тестовыми данными
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE context_items (key TEXT PRIMARY KEY, value TEXT, ts INTEGER NOT NULL)"
    )
    entries = [
        ("emotion:mood", "5", 300),  # посторонняя запись
        ("user:2", json.dumps({"text": "вторая"}, ensure_ascii=False), 200),
        ("misc", json.dumps({"text": "не нужна"}, ensure_ascii=False), 100),
        ("user:1", json.dumps({"text": "первая"}, ensure_ascii=False), 50),
    ]
    conn.executemany("INSERT INTO context_items VALUES (?, ?, ?)", entries)
    conn.commit()

    # Подменяем подключение к БД в модуле intel_status
    monkeypatch.setattr(intel_status, "get_connection", lambda: conn)

    with caplog.at_level("DEBUG"):
        items = intel_status._get_last_context_items(limit=2)

    # Проверяем, что вернулись только пользовательские записи в хронологическом порядке
    assert items == ["первая", "вторая"]

    # Убеждаемся, что лог содержит информацию о количестве отфильтрованных строк
    assert any("отфильтровано 2" in rec.message for rec in caplog.records)
