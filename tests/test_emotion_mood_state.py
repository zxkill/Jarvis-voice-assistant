import memory.db as db
from emotion.mood import Mood


def test_mood_update_ema_and_clamp(tmp_path, monkeypatch):
    """EMA‑сглаживание и кламп значений валентности и возбуждения."""
    cfg = tmp_path / "affect.yaml"
    cfg.write_text("valence_factor: 1\narousal_factor: 1\nema_alpha: 0.5\n", encoding="utf-8")

    mood = Mood(valence=0.0, arousal=0.0, config_path=cfg)
    mood.update(2.0, -2.0, trace_id="test")

    assert mood.valence == 0.5
    assert mood.arousal == -0.5


def test_mood_db_persistence(tmp_path, monkeypatch):
    """Сохранение и восстановление настроения в SQLite."""
    db_file = tmp_path / "memory.sqlite3"
    monkeypatch.setattr(db, "DB_PATH", db_file)

    db.set_mood_state(0.3, -0.7, trace_id="save")
    valence, arousal = db.get_mood_state(trace_id="load")

    assert valence == 0.3
    assert arousal == -0.7
