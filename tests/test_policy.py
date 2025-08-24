import datetime as dt

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
    now = dt.datetime(2024, 1, 1, 12, 0)
    assert policy.choose_channel(True, now=now) == "voice"
    now2 = now + dt.timedelta(seconds=30)
    assert policy.choose_channel(True, now=now2) is None
