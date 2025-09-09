import sys
from pathlib import Path

import time

from cryptography.fernet import Fernet

# Добавляем корень репозитория в путь импорта
sys.path.append(str(Path(__file__).resolve().parents[2]))

from memory import db as memory_db


def setup_db(monkeypatch, tmp_path):
    """Подготовить изолированную БД для тестов."""
    monkeypatch.setenv("JARVIS_DB_KEY", Fernet.generate_key().decode())
    db_file = tmp_path / "memory.sqlite3"
    monkeypatch.setattr(memory_db, "DB_PATH", db_file)
    return db_file


def test_clear_daily_digest(monkeypatch, tmp_path):
    setup_db(monkeypatch, tmp_path)
    with memory_db.get_connection() as conn:
        conn.execute(
            "INSERT INTO daily_digest (ts, digest) VALUES (?, 'a')",
            (int(time.time()),),
        )
        conn.execute(
            "INSERT INTO daily_digest (ts, digest) VALUES (?, 'b')",
            (int(time.time()),),
        )
    assert memory_db.clear_daily_digest() == 2
    with memory_db.get_connection() as conn:
        rows = conn.execute("SELECT COUNT(*) FROM daily_digest").fetchone()[0]
    assert rows == 0


def test_clear_episodic_memory(monkeypatch, tmp_path):
    setup_db(monkeypatch, tmp_path)
    with memory_db.get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO episodic_memory (ts, text, embedding, meta) VALUES (1, 't', '[]', '{}')"
        )
        eid = cur.lastrowid
        conn.execute(
            "INSERT INTO event_labels (event_id, label) VALUES (?, 'l')",
            (eid,),
        )
    assert memory_db.clear_episodic_memory() == 1
    with memory_db.get_connection() as conn:
        ecount = conn.execute("SELECT COUNT(*) FROM episodic_memory").fetchone()[0]
        lcount = conn.execute("SELECT COUNT(*) FROM event_labels").fetchone()[0]
    assert ecount == 0 and lcount == 0


def test_clear_semantic_memory(monkeypatch, tmp_path):
    setup_db(monkeypatch, tmp_path)
    with memory_db.get_connection() as conn:
        conn.execute(
            "INSERT INTO semantic_memory (ts, text, embedding, meta) VALUES (1, 't', '[]', '{}')"
        )
    assert memory_db.clear_semantic_memory() == 1
    with memory_db.get_connection() as conn:
        rows = conn.execute("SELECT COUNT(*) FROM semantic_memory").fetchone()[0]
    assert rows == 0
