"""Проверка записи истории настроения при изменении эмоций."""

import memory.db as db
from emotion.state import EmotionState


def test_history_recorded(tmp_path, monkeypatch):
    """Вызов ``raise_mood`` записывает строку в ``mood_history``."""

    # Используем временную БД, чтобы тест был изолированным
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "memory.sqlite3")

    state = EmotionState()
    state.raise_mood(10, reason="test")

    history = db.get_mood_history()
    assert len(history) == 1
    assert history[0]["source"] == "test"

