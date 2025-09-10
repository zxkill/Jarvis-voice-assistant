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

    db.set_mood({"valence": 0.3, "arousal": -0.7}, trace_id="save")
    mood = db.get_mood(trace_id="load")

    assert mood == {"valence": 0.3, "arousal": -0.7}


def test_mood_migration_and_legacy(tmp_path, monkeypatch):
    """Миграция со старых ключей и форматов значения настроения."""
    db_file = tmp_path / "memory.sqlite3"
    monkeypatch.setattr(db, "DB_PATH", db_file)

    # Создаём записи в устаревшем формате
    with db.get_connection() as conn:
        conn.execute(
            "INSERT INTO context_items (key, value, ts) VALUES (?, ?, 0)",
            (db.MOOD_KEY, "5"),
        )
        conn.execute(
            "INSERT INTO context_items (key, value, ts) VALUES (?, ?, 0)",
            (db._LEGACY_MOOD_STATE_KEY, "{\"valence\":0.1,\"arousal\":-0.2}"),
        )

    # Первый вызов должен мигрировать данные и вернуть новую структуру
    mood = db.get_mood()
    assert mood == {"valence": 0.1, "arousal": -0.2}

    # Повторный вызов не должен изменять данные
    mood2 = db.get_mood()
    assert mood2 == mood
