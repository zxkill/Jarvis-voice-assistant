import sys
from pathlib import Path

# Добавляем корень проекта в sys.path
sys.path.append(str(Path(__file__).resolve().parents[1]))

import core.events as events  # noqa: E402
from core.events import Event  # noqa: E402
from proactive.engine import ProactiveEngine  # noqa: E402
from proactive.policy import Policy, PolicyConfig  # noqa: E402
import skills.holiday_ru as holiday_ru  # noqa: E402
import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def clean_bus():
    events._subscribers.clear()
    events._global_subscribers.clear()


def _make_engine(monkeypatch):
    policy = Policy(PolicyConfig())
    engine = ProactiveEngine(policy)
    monkeypatch.setattr(policy, "choose_channel", lambda present, text=None: "voice")
    sent = {}

    def fake_send(channel, text, **kw):
        sent["text"] = text
        return True

    monkeypatch.setattr(engine, "_send", fake_send)
    processed = {}
    monkeypatch.setattr(engine, "_mark_processed", lambda sid: processed.setdefault("id", sid))
    return engine, sent, processed


def test_holiday_greeting_sent(monkeypatch):
    engine, sent, processed = _make_engine(monkeypatch)
    monkeypatch.setattr(holiday_ru, "is_day_off", lambda: (True, "Новый год"))
    monkeypatch.setattr("proactive.engine.is_day_off", lambda: (True, "Новый год"))
    event = Event(kind="suggestion.created", attrs={"text": "{holiday}", "reason_code": "holiday_greeting", "suggestion_id": 1})
    engine._on_suggestion(event)
    assert sent["text"] == "Новый год"
    assert processed["id"] == 1


def test_holiday_greeting_skipped(monkeypatch):
    engine, sent, processed = _make_engine(monkeypatch)
    monkeypatch.setattr(holiday_ru, "is_day_off", lambda: (False, ""))
    monkeypatch.setattr("proactive.engine.is_day_off", lambda: (False, ""))
    event = Event(kind="suggestion.created", attrs={"text": "{holiday}", "reason_code": "holiday_greeting", "suggestion_id": 2})
    engine._on_suggestion(event)
    assert sent == {}
    assert processed["id"] == 2
