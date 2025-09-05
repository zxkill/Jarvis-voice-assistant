"""Тесты плейбука и политики проактивных подсказок."""

import datetime as dt
import pathlib
import sys

sys.path.append(str(pathlib.Path(__file__).resolve().parents[2]))

from analysis import proactivity
from proactive.policy import Policy, PolicyConfig
from core import events


def test_playbook_contains_scenarios():
    pb = proactivity.load_playbook()
    assert "morning_briefing" in pb
    assert pb["morning_briefing"]["trigger"] == "time"


def test_trigger_generates_suggestion(monkeypatch):
    calls = {}

    def fake_act(prompt: str, trace_id: str | None = None) -> str:
        calls["prompt"] = prompt
        return "Привет"

    monkeypatch.setattr(proactivity.llm_engine, "act", fake_act)

    captured = {}

    def on_suggestion(event: events.Event) -> None:
        captured["text"] = event.attrs["text"]

    events.subscribe("suggestion.created", on_suggestion)
    events.fire_proactive_trigger("time", "morning_briefing")
    assert captured["text"] == "Привет"
    assert "утренний" in calls["prompt"].lower()


def test_policy_limits_and_keywords(monkeypatch):
    cfg = PolicyConfig(
        suggestion_min_interval_min=10,
        daily_limit=1,
        cancel_keywords={"стоп"},
    )
    policy = Policy(cfg)
    now = dt.datetime(2024, 1, 1, 12, 0)
    # первая отправка проходит
    assert policy.choose_channel(True, now=now, text="безопасно") == "voice"
    # повторная раньше интервала блокируется
    assert policy.choose_channel(True, now=now + dt.timedelta(minutes=1), text="безопасно") is None
    # ключевое слово отменяет отправку
    assert policy.choose_channel(True, now=now + dt.timedelta(minutes=20), text="стоп") is None
    # превышение дневного лимита
    assert policy.choose_channel(True, now=now + dt.timedelta(minutes=30), text="ещё") is None
