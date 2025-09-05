import sys
from pathlib import Path

import pytest

# Гарантируем, что корень репозитория в sys.path, иначе пакет `memory`
# может не обнаружиться при запуске теста из подпапки.
sys.path.append(str(Path(__file__).resolve().parents[2]))

from memory import db as memory_db
from memory.long_memory import retrieve_similar, store_event, store_fact
from memory import embeddings

@pytest.mark.parametrize("profile", ["simple", "transformer"])
def test_store_and_retrieve_similarity(tmp_path, monkeypatch, profile):
    """Эпизоды и факты должны находиться по запросу в разных профилях."""

    # Используем временную БД, чтобы не портить реальные данные
    monkeypatch.setattr(memory_db, "DB_PATH", tmp_path / "memory.sqlite3")

    # Подменяем профиль эмбеддингов: ``simple`` или ``transformer``.
    monkeypatch.setattr(embeddings, "EMBEDDING_PROFILE", profile)

    # Для трансформера подставляем фиктивную модель, чтобы тест не
    # зависел от внешних файлов. Модель использует тот же алгоритм,
    # что и fallback-хеширование, обеспечивая повторяемость результатов.
    if profile == "transformer":
        class DummyModel:
            def encode(self, texts):
                return [embeddings._hash_embedding(t) for t in texts]

        monkeypatch.setattr(embeddings, "_load_model", lambda: DummyModel())

    # Сохраняем несколько записей в обоих типах памяти
    store_event("пошёл в магазин за хлебом")
    store_event("слушал музыку")
    store_fact("Париж — столица Франции")

    # Проверяем, что запрос про магазин находит соответствующее событие
    event_results = retrieve_similar("магазин хлеб")
    assert event_results[0][0] == "пошёл в магазин за хлебом"

    # Проверяем, что факт про столицу корректно извлекается
    fact_results = retrieve_similar("столица Франции")
    assert fact_results[0][0] == "Париж — столица Франции"
