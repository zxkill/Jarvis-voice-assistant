import sys
from pathlib import Path

import pytest

# Добавляем корень репозитория в sys.path для корректного импорта модулей
sys.path.append(str(Path(__file__).resolve().parents[2]))

from app import scheduler
from core import llm_engine, events
from memory import db as memory_db


class DummyDailyMemory:
    """Заглушка дневной памяти для ускорения теста."""

    def fetch_all(self):
        return []

    def clear(self):
        pass


def test_nightly_reflection(monkeypatch, tmp_path):
    """Проверяем генерацию дайджеста, запись в БД и отправку уведомления."""

    # Используем временную БД, чтобы тест был изолирован
    db_file = tmp_path / "memory.sqlite3"
    monkeypatch.setattr(memory_db, "DB_PATH", db_file)

    # Заглушаем вызов LLM и возвращаем предсказуемый JSON-объект
    result = {"digest": "итоги дня", "priorities": "работа, отдых", "mood": 7}
    monkeypatch.setattr(llm_engine, "reflect", lambda: result)
    # Дневная память пуста — пересылка не требуется
    monkeypatch.setattr(scheduler, "daily_memory", DummyDailyMemory())

    # Перехватываем обновление настроения и приоритетов
    mood_holder = {}
    monkeypatch.setattr(
        memory_db,
        "set_mood",
        lambda mood: mood_holder.setdefault("mood", mood.get("level")),
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

    # Проверяем, что данные сохранились в таблице через новый API
    last = memory_db.get_last_digest()
    assert last["digest"] == "итоги дня"
    assert last["priorities"] == "работа, отдых"
    assert last["mood"] == 7
    # ``list_digests`` возвращает список, поэтому дополнительно проверяем его
    all_digests = memory_db.list_digests()
    assert len(all_digests) == 1
    assert mood_holder["mood"] == 7
    assert priorities_holder["priorities"] == "работа, отдых"

    # Убедимся, что событие для proactive.engine отправлено
    assert published and published[0].kind == "suggestion.created"
    assert published[0].attrs["text"] == "итоги дня"
    assert published[0].attrs["reason_code"] == "daily_digest"

