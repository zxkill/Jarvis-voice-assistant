import sys
from pathlib import Path

import pytest

# Гарантируем, что корень репозитория в sys.path, иначе пакет `memory`
# может не обнаружиться при запуске теста из подпапки.
sys.path.append(str(Path(__file__).resolve().parents[2]))

from memory import db as memory_db, embeddings
from memory.long_memory import store_fact
from memory.preferences import load_preferences, save_preference


@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
    """Использовать временную БД для изоляции тестов."""
    monkeypatch.setattr(memory_db, "DB_PATH", tmp_path / "memory.sqlite3")
    monkeypatch.setattr(embeddings, "EMBEDDING_PROFILE", "simple")


def test_roundtrip_preferences(temp_db):
    """Проверяем сохранение и загрузку пользовательских предпочтений."""

    # Сначала в памяти нет предпочтений
    assert load_preferences() == []

    pref_text = "я не ем хлеб"
    pref_id = save_preference(pref_text)
    assert isinstance(pref_id, int)

    # Обычный факт не должен попадать в список предпочтений
    store_fact("Париж — столица Франции")

    prefs = load_preferences()
    assert prefs == [pref_text]
