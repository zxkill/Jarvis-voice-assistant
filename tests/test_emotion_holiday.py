import types
import sys
from pathlib import Path

# Добавляем корень репозитория в путь для корректных импортов
sys.path.append(str(Path(__file__).resolve().parents[1]))

# Заглушки зависимостей, требующих аудио
sys.modules.setdefault("sounddevice", types.SimpleNamespace())
sys.modules.setdefault(
    "working_tts",
    types.SimpleNamespace(stop_speaking=lambda: None, speak_async=lambda *a, **k: None),
)

import core.events as core_events  # noqa: E402
from emotion.manager import EmotionManager  # noqa: E402
import emotion.mood as mood  # noqa: E402
import memory.db as db  # noqa: E402
import skills.holiday_ru as holiday_ru  # noqa: E402
import pytest  # noqa: E402
from emotion import policy  # noqa: E402


@pytest.fixture(autouse=True)
def clean_bus():
    core_events._subscribers.clear()
    core_events._global_subscribers.clear()
    policy._last_icon = None
    policy._last_switch_ts = 0.0


def _make_manager(monkeypatch, tmp_path, day_off_result):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "memory.sqlite3")

    def fake_load_config(self):
        self._valence_factor = 1.0
        self._arousal_factor = 1.0
        self._ema_alpha = 1.0

    monkeypatch.setattr(mood.Mood, "_load_config", fake_load_config)
    # Переопределяем функцию проверки праздника и внутри скилла, и в менеджере
    monkeypatch.setattr(holiday_ru, "is_day_off", lambda day=None: day_off_result)
    monkeypatch.setattr("emotion.manager.is_day_off", lambda day=None: day_off_result)

    class DummyTimer:
        def __init__(self, *a, **kw):
            pass

        def start(self):
            pass

        def cancel(self):
            pass

    monkeypatch.setattr("emotion.manager.Timer", DummyTimer)
    mgr = EmotionManager()
    return mgr


def test_holiday_boost(monkeypatch, tmp_path):
    mgr = _make_manager(monkeypatch, tmp_path, (True, "Новый год"))
    mgr.start()
    assert mgr._mood.valence > 0


def test_workday_no_boost(monkeypatch, tmp_path):
    mgr = _make_manager(monkeypatch, tmp_path, (False, ""))
    mgr.start()
    assert mgr._mood.valence == 0
