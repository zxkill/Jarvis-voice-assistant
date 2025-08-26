from datetime import datetime, time

from core.quiet import (
    QuietHours,
    derive_quiet_hours,
    refresh_quiet_hours,
    is_quiet_now,
)
from analysis import habits


def test_contains_cross_midnight():
    """Проверка корректной работы интервала, пересекающего полночь."""
    qh = QuietHours(start=time(23, 0), end=time(8, 0))
    assert qh.contains(datetime(2023, 1, 1, 23, 30))
    assert qh.contains(datetime(2023, 1, 2, 7, 59))
    assert not qh.contains(datetime(2023, 1, 2, 8, 0))
    assert not qh.contains(datetime(2023, 1, 2, 22, 59))


def test_is_quiet_now_monkeypatched(monkeypatch):
    """Функция ``is_quiet_now`` использует глобально загруженный интервал."""
    from core import quiet

    fake_now = datetime(2023, 1, 1, 12, 0, 0)

    class _DT(datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: D401 - совместимость с datetime
            return fake_now

    monkeypatch.setattr(quiet, "datetime", _DT)
    monkeypatch.setattr(quiet, "QUIET_HOURS", QuietHours(time(11, 0), time(13, 0)))
    assert is_quiet_now()


def test_derive_from_activity():
    """Автоматически определяем границы по собранной активности."""

    counts = [0] * 24
    for h in range(8, 22):  # активный день с 8 до 21
        counts[h] = 3600
    qh = derive_quiet_hours(counts)
    assert qh.start == time(22, 0)
    assert qh.end == time(8, 0)


def test_refresh_fallback_to_config(monkeypatch, tmp_path):
    """Если агрегатов нет, используются значения из config.ini."""

    cfg = tmp_path / "cfg.ini"
    cfg.write_text("[QUIET]\nstart=01:00\nend=02:00\n", encoding="utf-8")
    monkeypatch.setattr(habits, "load_last_aggregate", lambda: None)
    qh = refresh_quiet_hours(cfg)
    assert qh.start == time(1, 0)
    assert qh.end == time(2, 0)
