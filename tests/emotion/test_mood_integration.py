import sys
import pathlib
import pytest

# Добавляем корень репозитория в sys.path, чтобы корректно импортировать пакеты
sys.path.append(str(pathlib.Path(__file__).resolve().parents[2]))

import types

# Подменяем модуль notifiers.voice, чтобы не тянуть реальные зависимости TTS
sys.modules.setdefault("notifiers.voice", types.SimpleNamespace(send=lambda *a, **k: None))

from core import events as core_events
from core.events import Event

import emotion.mood as mood
import memory.db as db
from emotion.manager import EmotionManager


@pytest.fixture(autouse=True)
def clean_bus():
    """Очистить подписчиков перед каждым тестом."""
    core_events._subscribers.clear()
    core_events._global_subscribers.clear()


@pytest.fixture
def manager(monkeypatch, tmp_path):
    """Создаёт менеджер эмоций с заглушками для внешних зависимостей."""
    # Изолируем файл БД во временной директории
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "memory.sqlite3")

    # Настройки Mood: без сглаживания, чтобы значения менялись мгновенно
    def fake_load_config(self):
        self._valence_factor = 1.0
        self._arousal_factor = 1.0
        self._ema_alpha = 1.0
    monkeypatch.setattr(mood.Mood, "_load_config", fake_load_config)

    # Заглушка для таймера, чтобы тесты не ждали
    class DummyTimer:
        def __init__(self, *a, **k):
            pass
        def start(self):
            pass
        def cancel(self):
            pass
    monkeypatch.setattr("emotion.manager.Timer", DummyTimer)

    # Перехватываем вызовы llm_engine.mood
    calls: list[str] = []
    def fake_mood(feeling: str) -> str:
        calls.append(feeling)
        return "ok"
    monkeypatch.setattr("emotion.manager.llm_engine.mood", fake_mood)

    # Заглушки для TTS и дисплея
    monkeypatch.setattr("emotion.manager.voice.send", lambda *a, **k: None)
    class DummyDisplay:
        def draw(self, item):
            pass
        def process_events(self):
            pass
    monkeypatch.setattr("emotion.manager.get_driver", lambda: DummyDisplay())

    mgr = EmotionManager()
    mgr.start()
    yield mgr, calls
    mgr.stop()


def test_mood_integration(manager):
    mgr, calls = manager
    # Имитируем успешный и неуспешный диалоги, затем ночную рефлексию
    core_events.publish(Event(kind="dialog.success"))
    core_events.publish(Event(kind="dialog.failure"))
    core_events.publish(Event(kind="nightly_reflection.done"))

    # Проверяем, что llm_engine.mood вызван три раза с ожидаемыми причинами
    assert calls == [
        "после успешного диалога",
        "после неудачного диалога",
        "после ночной рефлексии",
    ]

    # В таблице mood_history должно быть по записи на каждое событие
    history = db.get_mood_history()
    assert len(history) == 3
    assert {h["source"] for h in history} == {
        "dialog.success",
        "dialog.failure",
        "nightly_reflection",
    }
