"""Тесты для навыка activity_by_hour."""

from skills import activity_by_hour as ab


def test_format_counts():
    counts = [60, 0, 120]
    assert ab._format_counts(counts) == "00:00 — 1 минута; 02:00 — 2 минуты"

