import datetime as dt

from core import metrics
from proactive.policy import Policy, PolicyConfig

def test_choose_channel_voice_by_default():
    policy = Policy(PolicyConfig())
    now = dt.datetime(2024, 1, 1, 12, 0)
    assert policy.choose_channel(present=True, now=now) == "voice"

def test_force_telegram_overrides_voice():
    policy = Policy(PolicyConfig(force_telegram=True))
    now = dt.datetime(2024, 1, 1, 12, 0)
    assert policy.choose_channel(present=True, now=now) == "telegram"

def test_choose_channel_absent_user():
    policy = Policy(PolicyConfig())
    now = dt.datetime(2024, 1, 1, 12, 0)
    assert policy.choose_channel(present=False, now=now) == "telegram"

def test_choose_channel_silence_window():
    start = dt.time(22, 0)
    end = dt.time(7, 0)
    policy = Policy(PolicyConfig(silence_window=(start, end)))
    now = dt.datetime(2024, 1, 1, 23, 0)
    assert policy.choose_channel(present=True, now=now) == "telegram"

def test_throttling_blocks_frequent_suggestions():
    policy = Policy(PolicyConfig(suggestion_min_interval_min=1))
    metrics.set_metric("policy.throttled", 0)
    now = dt.datetime(2024, 1, 1, 12, 0)
    assert policy.choose_channel(True, now=now) == "voice"
    now2 = now + dt.timedelta(seconds=30)
    assert policy.choose_channel(True, now=now2) is None
    assert metrics.get_metric("policy.throttled") == 1


def test_daily_limit_metric_incremented():
    policy = Policy(PolicyConfig(daily_limit=1))
    metrics.set_metric("policy.daily_limit_reached", 0)
    now = dt.datetime(2024, 1, 1, 12, 0)
    assert policy.choose_channel(True, now=now) == "voice"
    now2 = now + dt.timedelta(minutes=1)
    assert policy.choose_channel(True, now=now2) is None
    assert metrics.get_metric("policy.daily_limit_reached") == 1


def test_cancel_keyword_metric_incremented():
    cfg = PolicyConfig(cancel_keywords={"стоп"})
    policy = Policy(cfg)
    metrics.set_metric("policy.cancelled_keyword", 0)
    now = dt.datetime(2024, 1, 1, 12, 0)
    assert policy.choose_channel(True, now=now, text="стоп подсказка") is None
    assert metrics.get_metric("policy.cancelled_keyword") == 1
