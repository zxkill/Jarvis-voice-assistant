import time
import types
import asyncio
import sys
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

    def choose_channel(self, present: bool, *, now=None, text: str | None = None):  # type: ignore[override]
        """Всегда выбираем голосовой канал, игнорируя текст."""
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
    """Создать ``ProactiveEngine`` с подменой отправки уведомлений."""
    engine = ProactiveEngine(
        DummyPolicy(),
        response_timeout_sec=timeout,
    )
    # Перехватываем отправку уведомлений, чтобы тесты не делали сетевых вызовов.
    monkeypatch.setattr(engine, "_send", lambda *a, **k: True)
    return engine


def test_positive_response_speech(monkeypatch):
    engine = _engine(monkeypatch)
    feedback = []
    events = []
    core_events.subscribe("suggestion.response", lambda e: events.append(e))

    # Импортируем модуль команд после создания движка, чтобы он знал о нём.
    # ``app.command_processing`` тянет за собой зависимости ``sounddevice`` и
    # ``jarvis_skills``.  Подменяем их заглушками, чтобы тесты не требовали
    # установки тяжелых библиотек.
    sys.modules.setdefault("sounddevice", types.SimpleNamespace())
    sys.modules.setdefault(
        "jarvis_skills",
        types.SimpleNamespace(handle_utterance=lambda cmd: False, set_main_loop=lambda loop: None),
    )
    sys.modules.setdefault("core.nlp", types.SimpleNamespace(normalize=lambda x: x))
    sys.modules.setdefault("working_tts", types.SimpleNamespace(speak_async=lambda *a, **k: None))
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
    # Подменяем отправку подтверждений в Telegram, чтобы избежать реальных сетевых вызовов
    fake_tg = types.SimpleNamespace(send=lambda text: None)
    monkeypatch.setitem(sys.modules, "notifiers.telegram", fake_tg)
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


def test_positive_response_telegram(monkeypatch):
    """Проверяем, что положительный ответ через Telegram сохраняется и подтверждается."""
    engine = _engine(monkeypatch)
    feedback = []
    acks: list[str] = []
    # Заглушаем запись в БД и перехватываем текст подтверждения
    monkeypatch.setattr(
        "proactive.engine.add_suggestion_feedback",
        lambda sid, text, acc: feedback.append((sid, text, acc)),
    )
    fake_tg = types.SimpleNamespace(send=lambda text: acks.append(text))
    monkeypatch.setitem(sys.modules, "notifiers.telegram", fake_tg)

    core_events.publish(
        core_events.Event(
            kind="suggestion.created",
            attrs={"text": "выпей воды", "suggestion_id": 5, "trace_id": "trace-pos"},
        )
    )
    core_events.publish(
        core_events.Event(kind="telegram.message", attrs={"text": "да"})
    )

    assert feedback == [(5, "да", True)]
    assert acks and "записал" in acks[0].lower()


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
