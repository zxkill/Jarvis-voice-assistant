import time
import threading
import types
from collections import defaultdict

import pytest

from core import events as core_events
from proactive.engine import ProactiveEngine
from proactive.policy import Policy, PolicyConfig


class DummyPolicy(Policy):
    """Политика, всегда выбирающая голосовой канал."""

    def __init__(self):
        super().__init__(PolicyConfig())

    def choose_channel(self, present: bool, now=None):  # type: ignore[override]
        return "voice"


@pytest.fixture(autouse=True)
def reset_events(monkeypatch):
    """Сбрасываем подписчиков событий перед каждым тестом."""
    monkeypatch.setattr(core_events, "_subscribers", defaultdict(list))
    monkeypatch.setattr(core_events, "_global_subscribers", [])


def _engine(monkeypatch, timeout=1.0):
    # Блокируем запуск фонового потока ``_idle_loop``, чтобы тесты завершались
    # без лишних логов и зависших потоков. Для этого временно подменяем
    # ``threading.Thread`` на обёртку, которая игнорирует вызов ``start``.
    real_thread = threading.Thread

    def fake_thread(*args, **kwargs):
        target = kwargs.get("target")
        if target is not None:
            return types.SimpleNamespace(start=lambda: None)
        return real_thread(*args, **kwargs)

    monkeypatch.setattr(threading, "Thread", fake_thread)

    engine = ProactiveEngine(
        DummyPolicy(),
        idle_threshold_sec=10**6,
        smalltalk_interval_sec=10**6,
        check_period_sec=10**6,
        response_timeout_sec=timeout,
    )
    # Возвращаем оригинальный ``Thread`` для корректной работы ``Timer``.
    monkeypatch.setattr(threading, "Thread", real_thread)
    monkeypatch.setattr(engine, "_send", lambda *a, **k: True)
    return engine


def test_positive_response_speech(monkeypatch):
    engine = _engine(monkeypatch)
    feedback = []
    monkeypatch.setattr(
        "proactive.engine.add_suggestion_feedback",
        lambda sid, text, acc: feedback.append((sid, text, acc)),
    )
    events = []
    core_events.subscribe("suggestion.response", lambda e: events.append(e))

    core_events.publish(
        core_events.Event(
            kind="suggestion.created", attrs={"text": "выпей воды", "suggestion_id": 1}
        )
    )
    core_events.publish(
        core_events.Event(kind="speech.recognized", attrs={"text": "ок"})
    )
    time.sleep(0.1)

    assert feedback == [(1, "ок", True)]
    assert events and events[0].attrs["accepted"] is True


def test_negative_response_telegram(monkeypatch):
    engine = _engine(monkeypatch)
    feedback = []
    monkeypatch.setattr(
        "proactive.engine.add_suggestion_feedback",
        lambda sid, text, acc: feedback.append((sid, text, acc)),
    )
    core_events.publish(
        core_events.Event(
            kind="suggestion.created", attrs={"text": "зарядка", "suggestion_id": 2}
        )
    )
    core_events.publish(
        core_events.Event(kind="telegram.message", attrs={"text": "не сейчас"})
    )
    time.sleep(0.1)

    assert feedback == [(2, "не сейчас", False)]


def test_response_timeout(monkeypatch):
    engine = _engine(monkeypatch, timeout=0.1)
    feedback = []
    events = []
    monkeypatch.setattr(
        "proactive.engine.add_suggestion_feedback",
        lambda *a: feedback.append(a),
    )
    core_events.subscribe("suggestion.response", lambda e: events.append(e))
    core_events.publish(
        core_events.Event(
            kind="suggestion.created", attrs={"text": "позвони", "suggestion_id": 3}
        )
    )
    time.sleep(0.3)
    assert not feedback
    assert events == []
