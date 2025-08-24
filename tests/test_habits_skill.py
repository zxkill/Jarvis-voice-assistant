from __future__ import annotations

from __future__ import annotations

import datetime as dt

from analysis import habits
from skills import activity_by_hour


def test_save_and_load_last_aggregate(tmp_path, monkeypatch):
    file = tmp_path / "agg.json"
    monkeypatch.setattr(habits, "AGGREGATES_FILE", file)
    day = dt.date(2024, 1, 1)
    data = [0] * 24
    data[3] = 1800
    habits._save_daily_aggregate(day, data)
    assert habits.load_last_aggregate() == data


def test_activity_by_hour_skill(monkeypatch):
    data = [0] * 24
    data[5] = 1200  # 20 minutes at 05:00
    monkeypatch.setattr(activity_by_hour, "load_last_aggregate", lambda: data)
    res = activity_by_hour.handle("какая у меня активность по часам")
    assert "05:00" in res and "20 мин" in res
