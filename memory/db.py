"""Хранилище на SQLite и миграции для памяти Jarvis."""

from __future__ import annotations

# Стандартные библиотеки
import sqlite3
import time
import logging
import json
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
        processed INTEGER NOT NULL DEFAULT 0,
        reason_code TEXT
    )
    """,
    """
    ALTER TABLE suggestions ADD COLUMN reason_code TEXT
    """,
    """
    -- Таблица для хранения откликов пользователей на подсказки
    CREATE TABLE IF NOT EXISTS suggestion_feedback (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        suggestion_id INTEGER NOT NULL,
        response_text TEXT,
        accepted INTEGER NOT NULL,
        ts INTEGER NOT NULL,
        FOREIGN KEY (suggestion_id) REFERENCES suggestions(id) ON DELETE CASCADE
    )
    """,
    """
    -- Индекс ускоряет выборку отзывов по ID подсказки
    CREATE INDEX IF NOT EXISTS idx_suggestion_feedback_suggestion_id
        ON suggestion_feedback(suggestion_id)
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
    """
    -- Таблица для эпизодической памяти: хранит события с эмбеддингами
    CREATE TABLE IF NOT EXISTS episodic_memory (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts INTEGER NOT NULL,
        text TEXT NOT NULL,
        embedding TEXT NOT NULL,
        meta TEXT
    )
    """,
    """
    -- Таблица для семантической памяти: факты и знания
    CREATE TABLE IF NOT EXISTS semantic_memory (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts INTEGER NOT NULL,
        text TEXT NOT NULL,
        embedding TEXT NOT NULL,
        meta TEXT
    )
    """,
    """
    -- Хранение ежедневного дайджеста с приоритетами и настроением
    CREATE TABLE IF NOT EXISTS daily_digest (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts INTEGER NOT NULL,
        digest TEXT NOT NULL,
        priorities TEXT,
        mood INTEGER
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
    """Прогоняем DDL-миграции, игнорируя уже применённые шаги."""
    for ddl in SCHEMA:
        try:
            conn.execute(ddl)
        except sqlite3.OperationalError as exc:
            # Если столбец уже существует или таблица создана, SQLite выбросит
            # ``OperationalError``. Для идемпотентности миграций такие ошибки
            # подавляются, но логируются для отладки.
            logging.getLogger(__name__).debug("migration skipped: %s", exc)


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


# --- Small-talk timestamp helpers -----------------------------------------
SMALLTALK_KEY = "smalltalk:last_ts"
# Ключ для хранения уровня настроения ассистента
MOOD_LEVEL_KEY = "emotion:mood"
# Ключ для хранения valence/arousal настроения
MOOD_STATE_KEY = "emotion:mood_state"
# Ключ для хранения актуальных приоритетов на завтра
PRIORITIES_KEY = "reflection:priorities"


def get_last_smalltalk_ts() -> int:
    """Вернуть метку времени последнего small-talk.

    Используется проактивным движком, чтобы не надоедать пользователю
    слишком частыми репликами.
    """
    with get_connection() as conn:
        row = conn.execute(
            "SELECT value FROM context_items WHERE key=?", (SMALLTALK_KEY,)
        ).fetchone()
        return int(row["value"]) if row else 0


def set_last_smalltalk_ts(ts: int) -> None:
    """Сохранить момент времени последнего small-talk."""
    with get_connection() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO context_items (key, value, ts) VALUES (?, ?, ?)",
            (SMALLTALK_KEY, str(ts), ts),
        )


# --- Mood level helpers ----------------------------------------------------

def get_mood_level() -> int:
    """Вернуть сохранённый уровень настроения (по умолчанию 0)."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT value FROM context_items WHERE key=?", (MOOD_LEVEL_KEY,)
        ).fetchone()
        return int(row["value"]) if row else 0


def set_mood_level(level: int) -> None:
    """Сохранить текущий уровень настроения."""
    ts = int(time.time())
    with get_connection() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO context_items (key, value, ts) VALUES (?, ?, ?)",
            (MOOD_LEVEL_KEY, str(level), ts),
        )


# --- Reflection helpers ----------------------------------------------------

def set_priorities(priorities: str) -> None:
    """Сохранить список приоритетов на следующий день."""
    ts = int(time.time())
    with get_connection() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO context_items (key, value, ts) VALUES (?, ?, ?)",
            (PRIORITIES_KEY, priorities, ts),
        )


def add_daily_digest(digest: str, priorities: str | None, mood: int | None) -> int:
    """Записать результат вечерней рефлексии в отдельную таблицу."""
    ts = int(time.time())
    with get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO daily_digest (ts, digest, priorities, mood) VALUES (?, ?, ?, ?)",
            (ts, digest, priorities, mood),
        )
        return int(cur.lastrowid)


# --- Extended mood state helpers -------------------------------------------

def get_mood_state(trace_id: str | None = None) -> tuple[float, float]:
    """Вернуть сохранённое состояние настроения (valence, arousal)."""
    start = time.time()
    with get_connection() as conn:
        row = conn.execute(
            "SELECT value FROM context_items WHERE key=?", (MOOD_STATE_KEY,)
        ).fetchone()
        if row:
            data = json.loads(row["value"])
            valence = float(data.get("valence", 0.0))
            arousal = float(data.get("arousal", 0.0))
        else:
            valence, arousal = 0.0, 0.0
    duration = int((time.time() - start) * 1000)
    logging.getLogger(__name__).info(
        json.dumps(
            {
                "event": "db.get_mood_state",
                "trace_id": trace_id,
                "duration_ms": duration,
                "valence": valence,
                "arousal": arousal,
            },
            ensure_ascii=False,
        )
    )
    return valence, arousal


def set_mood_state(valence: float, arousal: float, trace_id: str | None = None) -> None:
    """Сохранить текущее состояние настроения (valence, arousal)."""
    start = time.time()
    ts = int(time.time())
    payload = json.dumps({"valence": valence, "arousal": arousal}, ensure_ascii=False)
    with get_connection() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO context_items (key, value, ts) VALUES (?, ?, ?)",
            (MOOD_STATE_KEY, payload, ts),
        )
    duration = int((time.time() - start) * 1000)
    logging.getLogger(__name__).info(
        json.dumps(
            {
                "event": "db.set_mood_state",
                "trace_id": trace_id,
                "duration_ms": duration,
                "valence": valence,
                "arousal": arousal,
            },
            ensure_ascii=False,
        )
    )
