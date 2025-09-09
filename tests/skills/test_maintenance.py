import sys
from pathlib import Path

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


def test_force_reflection(monkeypatch):
    called = False

    def fake_reflection():
        nonlocal called
        called = True

    monkeypatch.setattr(maintenance.scheduler, "_run_nightly_reflection", fake_reflection)
    reply = maintenance.handle("запусти рефлексию")
    assert called
    assert "Рефлексия" in reply


def test_clear_episodic(monkeypatch):
    monkeypatch.setattr(maintenance.memory_db, "clear_episodic_memory", lambda: 5)
    reply = maintenance.handle("очисти эпизодическую память")
    assert "Эпизодическая память очищена" in reply
