import datetime as dt
import time

import memory.db as db
from analysis import habits


def test_aggregate_by_hour(monkeypatch, tmp_path):
    db_file = tmp_path / "memory.sqlite3"
    monkeypatch.setattr(db, "DB_PATH", db_file)

    start = int(dt.datetime(2024, 1, 1, 23, 30).timestamp())
    end = int(dt.datetime(2024, 1, 2, 1, 30).timestamp())
    with db.get_connection() as conn:
        conn.execute(
            "INSERT INTO presence_sessions (start_ts, end_ts) VALUES (?, ?)",
            (start, end),
        )
    counts = habits.aggregate_by_hour()
    assert counts[23] == 30 * 60
    assert counts[0] == 60 * 60
    assert counts[1] == 30 * 60
