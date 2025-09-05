import sys
from pathlib import Path

import pytest

# Добавляем корень репозитория в sys.path для корректного импорта модулей
sys.path.append(str(Path(__file__).resolve().parents[2]))

from app import scheduler
from core import llm_engine, events
from memory import db as memory_db


def test_nightly_reflection(monkeypatch, tmp_path):
    """Проверяем генерацию дайджеста, запись в БД и отправку уведомления."""

    # Используем временную БД, чтобы тест был изолирован
    db_file = tmp_path / "memory.sqlite3"
    monkeypatch.setattr(memory_db, "DB_PATH", db_file)

    # Заглушаем вызов LLM и возвращаем предсказуемый результат
    result = {"digest": "итоги дня", "priorities": "работа, отдых", "mood": 7}
    monkeypatch.setattr(llm_engine, "reflect", lambda: result)

    # Перехватываем обновление настроения и приоритетов
    mood_holder = {}
    monkeypatch.setattr(
        memory_db, "set_mood_level", lambda level: mood_holder.setdefault("mood", level)
    )
    priorities_holder = {}
    monkeypatch.setattr(
        memory_db,
        "set_priorities",
        lambda text: priorities_holder.setdefault("priorities", text),
    )

    # Отслеживаем публикацию события для proactive.engine
    published: list[events.Event] = []
    monkeypatch.setattr(scheduler, "publish", lambda ev: published.append(ev))

    # Запускаем саму рефлексию
    scheduler._run_nightly_reflection()

    # Проверяем, что данные сохранились в таблице
    with memory_db.get_connection() as conn:
        row = conn.execute(
            "SELECT digest, priorities, mood FROM daily_digest"
        ).fetchone()

    assert dict(row) == {
        "digest": "итоги дня",
        "priorities": "работа, отдых",
        "mood": 7,
    }
    assert mood_holder["mood"] == 7
    assert priorities_holder["priorities"] == "работа, отдых"

    # Убедимся, что событие для proactive.engine отправлено
    assert published and published[0].kind == "suggestion.created"
    assert published[0].attrs["text"] == "итоги дня"
    assert published[0].attrs["reason_code"] == "daily_digest"

