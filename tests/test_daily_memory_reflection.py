import sys
from pathlib import Path
import types

import pytest
from cryptography.fernet import Fernet

# Добавляем корень репозитория в путь импорта
sys.path.append(str(Path(__file__).resolve().parents[1]))

from memory import db as memory_db


def test_daily_memory_transferred(monkeypatch, tmp_path):
    """Диалоги сохраняются в дневную память и переносятся ночью."""

    # Готовим изолированную БД и ключ шифрования
    monkeypatch.setenv("JARVIS_DB_KEY", Fernet.generate_key().decode())
    db_file = tmp_path / "memory.sqlite3"
    monkeypatch.setattr(memory_db, "DB_PATH", db_file)

    # Упрощаем NLP и голосовой нотифайер для импорта jarvis_skills
    fake_nlp = types.SimpleNamespace(normalize=lambda s: s)
    monkeypatch.setitem(sys.modules, "core.nlp", fake_nlp)
    fake_voice = types.SimpleNamespace(send=lambda *a, **k: None)
    monkeypatch.setitem(sys.modules, "notifiers.voice", fake_voice)
    # Подменяем модуль синтеза речи, чтобы избежать зависимости от PortAudio
    fake_tts = types.SimpleNamespace(speak_async=lambda *a, **k: None)
    monkeypatch.setitem(sys.modules, "working_tts", fake_tts)

    # Импортируем модули после подмен
    import jarvis_skills
    from app import command_processing, scheduler
    from context import daily_memory, long_term
    from core import llm_engine
    from memory import writer as memory_writer
    from core import events as core_events

    # Заглушки функций, которые не являются предметом теста
    monkeypatch.setattr(command_processing, "classify_feedback", lambda s, u, t: (True, ""))
    monkeypatch.setattr(memory_writer, "add_suggestion_feedback", lambda *a, **k: 0)
    monkeypatch.setattr(command_processing, "inc_metric", lambda *a, **k: None)
    monkeypatch.setattr(command_processing, "pop_awaiting", lambda: {"id": 1, "text": "напоминание", "channel": "voice", "trace_id": "t"})
    monkeypatch.setattr(command_processing.asyncio, "create_task", lambda coro: None)

    # Фиксируем публикацию событий, чтобы не обращаться к внешним системам
    monkeypatch.setattr(core_events, "publish", lambda *a, **k: None)
    monkeypatch.setattr(scheduler, "publish", lambda *a, **k: None)

    saved_events = []

    def fake_add_daily_event(text, labels):
        saved_events.append((text, list(labels)))
        return "k"

    monkeypatch.setattr(long_term, "add_daily_event", fake_add_daily_event)

    def fake_summarise(text, labels=None):
        summary = f"sum:{text}"
        fake_add_daily_event(summary, labels or ["summary"])
        return summary

    monkeypatch.setattr(llm_engine, "summarise", fake_summarise)
    monkeypatch.setattr(llm_engine, "reflect", lambda: {"digest": "d", "priorities": None, "mood": None})

    monkeypatch.setattr(memory_db, "add_daily_digest", lambda *a, **k: None)
    monkeypatch.setattr(memory_db, "set_priorities", lambda *a, **k: None)
    monkeypatch.setattr(memory_db, "set_mood_level", lambda *a, **k: None)

    # Записываем подсказку в БД для получения reason_code
    with memory_db.get_connection() as conn:
        conn.execute(
            "INSERT INTO suggestions (id, text, ts, processed, reason_code) VALUES (1, 'пей воду', 0, 0, 'drink_water')"
        )

    # Регистрируем тестовый скилл
    def skill(text, trace_id=None):
        return "ок"

    skill.__module__ = "my_birthday"
    jarvis_skills._loaded = [(["привет"], skill)]

    # Имитация пользовательской реплики и ответа на подсказку
    assert jarvis_skills.handle_utterance("привет") is True
    command_processing.process_suggestion_answer("хорошо")

    # Проверяем, что дневная память содержит обе записи
    records = daily_memory.fetch_all()
    assert len(records) == 2

    # Запускаем ночную рефлексию и убеждаемся, что записи перенесены
    scheduler._run_nightly_reflection()

    assert daily_memory.fetch_all() == []
    assert len(saved_events) == 3
    assert set(saved_events[-1][1]) == {"my_birthday", "drink_water"}
