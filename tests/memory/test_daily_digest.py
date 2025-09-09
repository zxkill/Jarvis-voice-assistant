import sys
from pathlib import Path

import pytest

# Добавляем корень репозитория в путь импорта
sys.path.append(str(Path(__file__).resolve().parents[2]))

from memory import db as memory_db  # noqa: E402


def test_digest_utils(monkeypatch, tmp_path):
    """Проверяем операции чтения и очистки дневных дайджестов."""

    # Используем отдельный файл БД для изоляции теста
    monkeypatch.setattr(memory_db, "DB_PATH", tmp_path / "memory.sqlite3")

    # Фиксируем время для предсказуемых меток
    monkeypatch.setattr(memory_db.time, "time", lambda: 1000)
    memory_db.add_daily_digest("d1", "p1", 1)
    monkeypatch.setattr(memory_db.time, "time", lambda: 2000)
    memory_db.add_daily_digest("d2", "p2", 2)

    # ``get_last_digest`` возвращает последнюю запись
    last = memory_db.get_last_digest()
    assert last["digest"] == "d2"

    # ``list_digests`` выдаёт обе записи в порядке убывания времени
    digests = memory_db.list_digests()
    assert [d["digest"] for d in digests] == ["d2", "d1"]
    assert [d["digest"] for d in memory_db.list_digests(limit=1)] == ["d2"]

    # Очистка старых записей с нулевым порогом удаляет первую запись
    removed = memory_db.cleanup_old_digests(retention_days=0)
    assert removed == 1
    assert [d["digest"] for d in memory_db.list_digests()] == ["d2"]

    # Увеличиваем время и удаляем оставшуюся запись
    monkeypatch.setattr(memory_db.time, "time", lambda: 3000)
    removed = memory_db.cleanup_old_digests(retention_days=0)
    assert removed == 1
    assert memory_db.get_last_digest() is None


def test_digest_rotation_by_count(monkeypatch, tmp_path):
    """Проверяем ротацию дайджестов по лимиту количества."""

    # Изолируем тестовую БД
    monkeypatch.setattr(memory_db, "DB_PATH", tmp_path / "memory.sqlite3")

    # Используем переменную для контроля времени
    current_ts = 1000

    def fake_time():
        return current_ts

    monkeypatch.setattr(memory_db.time, "time", fake_time)

    # Добавляем три дайджеста с разными метками времени
    for idx in range(3):
        memory_db.add_daily_digest(f"d{idx}", None, None)
        current_ts += 1000

    # Оставляем только два последних элемента
    removed = memory_db.cleanup_old_digests(retention_days=9999, max_count=2)
    assert removed == 1

    # В базе должны остаться только самые свежие записи
    digests = memory_db.list_digests()
    assert [d["digest"] for d in digests] == ["d2", "d1"]

