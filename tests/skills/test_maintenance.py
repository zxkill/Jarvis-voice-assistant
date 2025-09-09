import sys
from pathlib import Path

import pytest

# Добавляем корень репозитория в путь импорта
sys.path.append(str(Path(__file__).resolve().parents[2]))

from skills import maintenance


def test_clear_daily_memory(monkeypatch):
    called = False

    def fake_clear():
        nonlocal called
        called = True

    monkeypatch.setattr(maintenance.daily_memory, "clear", fake_clear)
    reply = maintenance.handle("очисти дневную память")
    assert called
    assert "очищена" in reply


@pytest.mark.parametrize("command", ["запусти рефлексию", "агрегируй данные"])
def test_force_reflection(monkeypatch, command):
    called = False

    def fake_reflection():
        nonlocal called
        called = True

    monkeypatch.setattr(maintenance.scheduler, "_run_nightly_reflection", fake_reflection)
    reply = maintenance.handle(command)
    assert called
    assert "Рефлексия" in reply


def test_reflection_failure(monkeypatch):
    """При ошибке рефлексии скилл должен сообщить об этом, а не падать."""

    def bad_reflection():
        raise ValueError("digest")

    # Подменяем функцию планировщика, чтобы она бросала исключение
    monkeypatch.setattr(maintenance.scheduler, "_run_nightly_reflection", bad_reflection)

    logged = {}

    def fake_exception(msg, *, extra=None):
        logged["msg"] = msg

    # Перехватываем логгер, чтобы убедиться в регистрации ошибки
    monkeypatch.setattr(maintenance.logger, "exception", fake_exception)

    reply = maintenance.handle("агрегируй данные")
    assert "Не удалось" in reply
    assert logged["msg"] == "nightly reflection failed"


def test_clear_episodic(monkeypatch):
    monkeypatch.setattr(maintenance.memory_db, "clear_episodic_memory", lambda: 5)
    reply = maintenance.handle("очисти эпизодическую память")
    assert "Эпизодическая память очищена" in reply
