import time
import threading
import types
import asyncio
from collections import defaultdict
import inspect
from collections import namedtuple

# Совместимость со старыми зависимостями, использующими ``inspect.getargspec``
if not hasattr(inspect, "getargspec"):
    ArgSpec = namedtuple("ArgSpec", "args varargs keywords defaults")

    def getargspec(func):  # type: ignore
        fs = inspect.getfullargspec(func)
        return ArgSpec(fs.args, fs.varargs, fs.varkw, fs.defaults)

    inspect.getargspec = getargspec  # type: ignore[attr-defined]

import pytest

from core import events as core_events
from core import metrics as core_metrics
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


@pytest.fixture(autouse=True)
def reset_metrics():
    """Очищаем реестр метрик перед каждым тестом."""
    core_metrics._metrics.clear()
    yield


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
    events = []
    core_events.subscribe("suggestion.response", lambda e: events.append(e))

    # Импортируем модуль команд после создания движка, чтобы он знал о нём.
    # Модуль ``app.command_processing`` зависит от ``sounddevice`` (PortAudio),
    # поэтому подменяем его заглушкой, чтобы избежать необходимости
    # системной библиотеки в тестах.
    import sys
    sys.modules.setdefault("sounddevice", types.SimpleNamespace())
    import app.command_processing as cp

    monkeypatch.setattr(cp, "handle_utterance", lambda cmd: False)
    monkeypatch.setattr(cp, "execute_cmd", lambda cmd, voice: False)
    monkeypatch.setattr(cp, "normalize", lambda x: x)
    monkeypatch.setattr(cp, "add_suggestion_feedback", lambda sid, text, acc: feedback.append((sid, text, acc)))

    trace_id = "test-trace-positive"
    core_events.publish(
        core_events.Event(
            kind="suggestion.created",
            attrs={"text": "выпей воды", "suggestion_id": 1, "trace_id": trace_id},
        )
    )

    # Ответ пользователя обрабатывается через ``va_respond`` и не попадает
    # в общую цепочку команд.
    asyncio.run(cp.va_respond("джарвис ок"))

    assert feedback == [(1, "ок", True)]
    assert events and events[0].attrs["accepted"] is True
    assert events[0].attrs["trace_id"] == trace_id
    assert core_metrics.get_metric("suggestions.responded") == 1.0
    assert core_metrics.get_metric("suggestions.accepted") == 1.0
    assert core_metrics.get_metric("suggestions.declined") == 0.0


def test_negative_response_telegram(monkeypatch):
    engine = _engine(monkeypatch)
    feedback = []
    events: list[core_events.Event] = []
    core_events.subscribe("suggestion.response", lambda e: events.append(e))
    monkeypatch.setattr(
        "proactive.engine.add_suggestion_feedback",
        lambda sid, text, acc: feedback.append((sid, text, acc)),
    )
    trace_id = "test-trace-negative"
    core_events.publish(
        core_events.Event(
            kind="suggestion.created",
            attrs={"text": "зарядка", "suggestion_id": 2, "trace_id": trace_id},
        )
    )
    core_events.publish(
        core_events.Event(kind="telegram.message", attrs={"text": "не сейчас"})
    )
    time.sleep(0.1)

    assert feedback == [(2, "не сейчас", False)]
    assert events and events[0].attrs["trace_id"] == trace_id
    assert events[0].attrs["accepted"] is False
    assert core_metrics.get_metric("suggestions.responded") == 1.0
    assert core_metrics.get_metric("suggestions.accepted") == 0.0
    assert core_metrics.get_metric("suggestions.declined") == 1.0


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
            kind="suggestion.created",
            attrs={"text": "позвони", "suggestion_id": 3, "trace_id": "trace-timeout"},
        )
    )
    time.sleep(0.3)
    assert not feedback
    assert events == []
    assert core_metrics.get_metric("suggestions.responded") == 0.0
