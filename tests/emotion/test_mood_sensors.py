import datetime as _dt

from emotion.mood import Mood
from emotion.sensors import TimeOfDayMoodSensor


def _make_cfg(tmp_path):
    """Создать конфигурацию без сглаживания для точных тестов."""
    cfg = tmp_path / "affect.yaml"
    cfg.write_text("valence_factor: 1\narousal_factor: 1\nema_alpha: 1\n", encoding="utf-8")
    return cfg


def test_morning_increases_mood(tmp_path, monkeypatch):
    """Утром настроение должно повышаться."""
    cfg = _make_cfg(tmp_path)
    mood = Mood(valence=0.0, arousal=0.0, config_path=cfg)
    # Запрещаем обращение к реальной БД
    monkeypatch.setattr(mood, "save", lambda trace_id=None: None)

    sensor = TimeOfDayMoodSensor(mood, clock=lambda: _dt.datetime(2024, 1, 1, 8, 0, 0))
    sensor.read_and_update()

    assert mood.valence == 0.2
    assert mood.arousal == 0.3


def test_noon_no_change(tmp_path, monkeypatch):
    """Днём сенсор не должен менять настроение."""
    cfg = _make_cfg(tmp_path)
    mood = Mood(valence=0.0, arousal=0.0, config_path=cfg)
    monkeypatch.setattr(mood, "save", lambda trace_id=None: None)
    calls: list[tuple[float, float]] = []
    monkeypatch.setattr(mood, "update", lambda v, a, trace_id=None: calls.append((v, a)))

    sensor = TimeOfDayMoodSensor(mood, clock=lambda: _dt.datetime(2024, 1, 1, 15, 0, 0))
    sensor.read_and_update()

    assert calls == []
    assert mood.valence == 0.0
    assert mood.arousal == 0.0


def test_night_decreases_mood(tmp_path, monkeypatch):
    """Вечером и ночью настроение понижается."""
    cfg = _make_cfg(tmp_path)
    mood = Mood(valence=0.0, arousal=0.0, config_path=cfg)
    monkeypatch.setattr(mood, "save", lambda trace_id=None: None)

    sensor = TimeOfDayMoodSensor(mood, clock=lambda: _dt.datetime(2024, 1, 1, 22, 0, 0))
    sensor.read_and_update()

    assert mood.valence == -0.2
    assert mood.arousal == -0.3
