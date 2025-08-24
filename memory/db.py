"""Хранилище на SQLite и миграции для памяти Jarvis."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

# Путь к файлу БД, создаётся рядом с модулем
DB_PATH = Path(__file__).with_name("memory.sqlite3")

# SQL‑скрипты для создания таблиц и индексов
SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts INTEGER NOT NULL,
        event_type TEXT NOT NULL,
        payload TEXT
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts)
    """,
    """
    CREATE TABLE IF NOT EXISTS presence_sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT,
        start_ts INTEGER NOT NULL,
        end_ts INTEGER
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS suggestions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        text TEXT NOT NULL,
        ts INTEGER NOT NULL,
        processed INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS timers (
        label   TEXT PRIMARY KEY,
        typ     TEXT NOT NULL,
        end_ts  INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS context_items (
        key TEXT PRIMARY KEY,
        value TEXT,
        ts INTEGER NOT NULL
    )
    """,
]

# Удерживаем события не дольше двух недель
RETENTION_SECONDS = 14 * 24 * 3600  # две недели


def get_connection() -> sqlite3.Connection:
    """Вернуть подключение SQLite с миграциями и ротацией."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    _migrate(conn)  # выполняем миграции при каждом подключении
    _rotate_events(conn)  # удаляем старые события
    _cleanup_timers(conn)  # удаляем истекшие таймеры
    conn.commit()
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Прогоняем DDL-миграции."""
    for ddl in SCHEMA:
        conn.execute(ddl)


def _rotate_events(conn: sqlite3.Connection) -> None:
    """Удаляем из таблицы events записи старше порога."""
    cutoff = int(time.time() - RETENTION_SECONDS)
    conn.execute("DELETE FROM events WHERE ts < ?", (cutoff,))


def _cleanup_timers(conn: sqlite3.Connection) -> None:
    """Удаляем старые записи о таймерах из таблицы ``timers``.

    Таймеры остаются в базе до подтверждения пользователем, поэтому
    чистим только те, что завершились более суток назад.
    """
    now = int(time.time())
    cutoff = now - 24 * 3600  # оставляем информацию за последние 24 часа
    conn.execute("DELETE FROM timers WHERE end_ts <= ?", (cutoff,))
