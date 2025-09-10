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
    # Подменяем сохранение в БД, чтобы тест был детерминированным
    monkeypatch.setattr(proactivity, "add_suggestion", lambda text, code: 42)

    captured = {}

    def on_suggestion(event: events.Event) -> None:
        captured["text"] = event.attrs["text"]
        captured["id"] = event.attrs["suggestion_id"]

    events._subscribers.clear()
    events.subscribe("proactivity.trigger", proactivity._handle_trigger)
    events.subscribe("suggestion.created", on_suggestion)
    # fire_proactive_trigger теперь возвращает поток, который необходимо
    # дождаться, чтобы обработчик успел опубликовать событие
    thread = events.fire_proactive_trigger("time", "morning_briefing")
    thread.join(timeout=2)
    assert captured["text"].startswith("Сформируй краткий утренний брифинг")
    assert captured["id"] == 42


def test_policy_limits_and_keywords(monkeypatch):
    cfg = PolicyConfig(
        suggestion_min_interval_min=10,
        daily_limit=1,
        cancel_keywords={"стоп"},
    )
    policy = Policy(cfg)
    monkeypatch.setattr("proactive.policy.is_quiet_now", lambda: False)
    now = dt.datetime(2024, 1, 1, 12, 0)
    # первая отправка проходит
    assert policy.choose_channel(True, now=now, text="безопасно") == "voice"
    # повторная раньше интервала блокируется
    assert policy.choose_channel(True, now=now + dt.timedelta(minutes=1), text="безопасно") is None
    # ключевое слово отменяет отправку
    assert policy.choose_channel(True, now=now + dt.timedelta(minutes=20), text="стоп") is None
    # превышение дневного лимита
    assert policy.choose_channel(True, now=now + dt.timedelta(minutes=30), text="ещё") is None
