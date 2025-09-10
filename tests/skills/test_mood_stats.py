from pathlib import Path
import sys

# Добавляем корень репозитория в PYTHONPATH для корректных импортов
sys.path.append(str(Path(__file__).resolve().parents[2]))

from skills import mood_stats


def test_handle_returns_summary(monkeypatch):
    """Скилл должен корректно агрегировать и форматировать статистику."""

    data = [
        {"valence": 0.1, "arousal": 0.2},
        {"valence": -0.3, "arousal": 0.4},
        {"valence": 0.0, "arousal": -0.2},
    ]
    monkeypatch.setattr(mood_stats.db, "get_mood_history", lambda limit=1000: data)

    reply = mood_stats.handle("статистика настроения")

    assert "Всего записей: 3" in reply
    assert "средняя валентность -0.07" in reply
    assert "среднее возбуждение 0.13" in reply
    assert "последнее состояние 0.10/0.20" in reply
