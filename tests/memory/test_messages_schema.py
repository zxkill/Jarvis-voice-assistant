# -*- coding: utf-8 -*-
"""Проверка миграции универсальной таблицы сообщений в памяти Jarvis."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

# Добавляем корневую директорию проекта в ``sys.path``, чтобы тесты могли
# импортировать пакет ``memory`` без предварительной установки на машину.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from memory.sqlite.messages_schema import initialize_messages_schema, MESSAGE_SCHEMA


@pytest.fixture()
def sqlite_conn() -> sqlite3.Connection:
    """Создать изолированное in-memory подключение SQLite для проверки DDL."""

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return conn


def test_initialize_messages_schema_creates_table_and_indexes(sqlite_conn: sqlite3.Connection) -> None:
    """Миграция должна создавать таблицу и все индексные структуры."""

    initialize_messages_schema(sqlite_conn)

    # Проверяем столбцы таблицы сообщений. PRAGMA table_info возвращает
    # последовательность с именами и типами столбцов.
    columns = {row["name"]: row["type"] for row in sqlite_conn.execute("PRAGMA table_info(messages)")}
    assert columns == {
        "id": "INTEGER",
        "trace_id": "TEXT",
        "ts": "INTEGER",
        "direction": "TEXT",
        "channel": "TEXT",
        "payload": "TEXT",
        "metadata": "TEXT",
    }

    # Список индексов и их уникальность. Все индексы в схеме не уникальны.
    indexes = {row["name"]: row["unique"] for row in sqlite_conn.execute("PRAGMA index_list(messages)")}
    assert indexes == {
        "idx_messages_ts": 0,
        "idx_messages_trace_ts": 0,
        "idx_messages_direction_ts": 0,
        "idx_messages_channel_ts": 0,
    }


def test_initialize_messages_schema_is_idempotent(sqlite_conn: sqlite3.Connection) -> None:
    """Повторный запуск миграции не должен приводить к ошибкам."""

    initialize_messages_schema(sqlite_conn)
    # Второй запуск должен пройти без исключений. Для надёжности используем
    # альтернативный список команд, чтобы покрыть ветку с параметром ``statements``.
    initialize_messages_schema(sqlite_conn, statements=MESSAGE_SCHEMA)

    # Проверяем, что таблица осталась доступна.
    row = sqlite_conn.execute("SELECT COUNT(*) AS cnt FROM messages").fetchone()
    assert row["cnt"] == 0
