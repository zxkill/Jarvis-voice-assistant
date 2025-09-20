# -*- coding: utf-8 -*-
"""DDL-скрипты и инициализация таблиц для универсального лога сообщений."""

from __future__ import annotations

import sqlite3
from typing import Iterable

from core.logging_json import configure_logging

# Создаём именованный логгер, чтобы было проще отфильтровать вывод
# при отладке миграций БД. Логи записывают идентификаторы выполняемых
# шагов и помогают понять, на каком из них произошла ошибка.
log = configure_logging("memory.sqlite.messages")

# ``MESSAGE_SCHEMA`` содержит idempotent-скрипты, которые можно выполнять
# при каждом открытии соединения. SQLite просто проигнорирует операции
# ``CREATE``/``CREATE INDEX``, если объект уже существует.
MESSAGE_SCHEMA: tuple[str, ...] = (
    """
    -- Основная таблица для хранения сообщений диалогов и системных событий
    CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT, -- уникальный идентификатор записи
        trace_id TEXT NOT NULL DEFAULT '',    -- связывает сообщения в одну сессию
        ts INTEGER NOT NULL,                  -- Unix-время создания сообщения
        direction TEXT NOT NULL,              -- направление: incoming/outgoing/system
        channel TEXT NOT NULL DEFAULT '',     -- источник сообщения (telegram, voice)
        payload TEXT NOT NULL,                -- тело сообщения в исходном формате
        metadata TEXT                         -- дополнительный JSON с метаданными
    )
    """,
    """
    -- Индекс по временной метке ускоряет сортировку и выборку последних сообщений
    CREATE INDEX IF NOT EXISTS idx_messages_ts
        ON messages(ts)
    """,
    """
    -- Композитный индекс trace_id + ts ускоряет выборку диалога по сессии
    CREATE INDEX IF NOT EXISTS idx_messages_trace_ts
        ON messages(trace_id, ts)
    """,
    """
    -- Индекс по направлению и времени помогает фильтровать исходящие/входящие сообщения
    CREATE INDEX IF NOT EXISTS idx_messages_direction_ts
        ON messages(direction, ts)
    """,
    """
    -- Индекс по каналу и времени облегчает аналитические отчёты по источникам
    CREATE INDEX IF NOT EXISTS idx_messages_channel_ts
        ON messages(channel, ts)
    """,
)


def initialize_messages_schema(conn: sqlite3.Connection, *, statements: Iterable[str] | None = None) -> None:
    """Применить DDL-скрипты для инициализации таблицы ``messages``.

    Функция принимает подключение SQLite и выполняет набор SQL-команд.
    По умолчанию используется ``MESSAGE_SCHEMA``, но можно передать свой
    список ``statements`` для тестов. Каждый шаг логируется с указанием
    первых символов скрипта, чтобы при анализе логов быстро находить
    потенциально проблемную команду.
    """

    ddl_source = tuple(statements) if statements is not None else MESSAGE_SCHEMA
    for ddl in ddl_source:
        trimmed = ddl.strip().splitlines()[0] if ddl.strip() else "<empty>"
        log.debug(
            "выполняем миграцию сообщения", extra={"ctx": {"snippet": trimmed[:60]}}
        )
        try:
            conn.execute(ddl)
        except sqlite3.Error:
            log.exception(
                "ошибка применения миграции сообщений", extra={"ctx": {"snippet": trimmed}}
            )
            raise

    log.debug("миграции сообщений успешно выполнены")
