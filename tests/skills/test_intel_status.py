import sys
from pathlib import Path

import pytest

# Добавляем корень репозитория в sys.path, чтобы корректно импортировать пакеты
sys.path.append(str(Path(__file__).resolve().parents[2]))

from skills import intel_status  # noqa: E402


class CallRecorder:
    """Простая обёртка для фиксации вызовов и аргументов."""

    def __init__(self):
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))


@pytest.fixture()
def recorders(monkeypatch):
    """Подмена функций сохранения, чтобы отслеживать их вызовы."""

    pref = CallRecorder()
    note = CallRecorder()
    monkeypatch.setattr(intel_status, "save_preference", pref)
    monkeypatch.setattr(intel_status, "add_daily_event", note)
    return pref, note


def test_handle_saves_preference(recorders):
    """Фраза вида «запомни, что ...» должна сохраняться как предпочтение."""

    pref, note = recorders
    reply = intel_status.handle("запомни, что я не ем хлеб")
    assert reply == "Запомнил"
    assert pref.calls == [(("я не ем хлеб",), {})]
    assert note.calls == []


def test_handle_saves_note(recorders):
    """Обычная фраза после «запомни» должна сохраняться как заметка."""

    pref, note = recorders
    reply = intel_status.handle("запомни купить молоко")
    assert reply == "Запомнил"
    assert pref.calls == []
    assert note.calls == [(("купить молоко", [intel_status.LABEL]), {})]
