"""Тесты для ``utils.rate_limiter``."""

from __future__ import annotations

from utils.rate_limiter import RateLimiter


def test_rate_limiter_blocks_excess_calls() -> None:
    """После превышения лимита вызов запрещается."""
    times = iter([0, 0.2, 0.4])
    rl = RateLimiter(2, 1, time_func=lambda: next(times))
    assert rl.allow()
    assert rl.allow()
    assert not rl.allow()


def test_rate_limiter_resets_after_period() -> None:
    """Через период лимит сбрасывается."""
    times = iter([0, 0.2, 1.2])
    rl = RateLimiter(1, 1, time_func=lambda: next(times))
    assert rl.allow()
    assert not rl.allow()
    assert rl.allow()
