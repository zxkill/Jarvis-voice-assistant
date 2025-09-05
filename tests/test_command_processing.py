"""Тесты модуля ``app.command_processing``.

Модуль активно зависит от ``jarvis_skills``, ``core.nlp`` и синтезатора
речи, поэтому для юнит‑тестов эти зависимости подменяются простыми
заглушками. Так мы проверяем только собственную логику разбора
команд, не затрагивая тяжёлые внешние пакеты.
"""

import sys
import asyncio
from types import SimpleNamespace

import pytest

from core.request_source import set_request_source, reset_request_source


def _load_cp(monkeypatch):
    """Импортировать ``command_processing`` с подменой зависимостей."""

    dummy_skills = SimpleNamespace(handle_utterance=lambda cmd: False)
    dummy_nlp = SimpleNamespace(normalize=lambda x: x)
    dummy_tts = SimpleNamespace(speak_async=lambda *a, **k: None)
    monkeypatch.setitem(sys.modules, "jarvis_skills", dummy_skills)
    monkeypatch.setitem(sys.modules, "core.nlp", dummy_nlp)
    monkeypatch.setitem(sys.modules, "working_tts", dummy_tts)
    monkeypatch.delitem(sys.modules, "app.command_processing", raising=False)
    import app.command_processing as cp
    return cp


def test_extract_cmd_requires_alias_for_voice(monkeypatch):
    """Без слова «Джарвис» голосовая фраза игнорируется."""

    cp = _load_cp(monkeypatch)
    assert cp.extract_cmd("включи свет") == ""
    assert cp.extract_cmd("джарвис включи свет") == "включи свет"


def test_extract_cmd_no_alias_needed_for_telegram(monkeypatch):
    """В Telegram имя ассистента можно опустить."""

    cp = _load_cp(monkeypatch)
    token = set_request_source("telegram")
    try:
        assert cp.extract_cmd("включи свет") == "включи свет"
    finally:
        reset_request_source(token)


def test_va_respond_processes_telegram_without_alias(monkeypatch):
    """``va_respond`` должен передать текст навыкам без слова активации."""

    cp = _load_cp(monkeypatch)
    called = {}

    def fake_handle(cmd: str) -> bool:
        called["cmd"] = cmd
        return True

    monkeypatch.setattr(cp, "handle_utterance", fake_handle)

    async def run():
        token = set_request_source("telegram")
        try:
            assert await cp.va_respond("проверка") is True
        finally:
            reset_request_source(token)

    asyncio.run(run())
    assert called["cmd"] == "проверка"


def test_suggestion_answer_bypasses_handlers(monkeypatch):
    """Ответ на подсказку не должен попадать в обычный обработчик команд."""

    cp = _load_cp(monkeypatch)

    from proactive.engine import ProactiveEngine
    from proactive.policy import Policy, PolicyConfig

    class DummyPolicy(Policy):
        def __init__(self) -> None:
            super().__init__(PolicyConfig())

        def choose_channel(self, present: bool, *, now=None, text: str | None = None):  # type: ignore[override]
            """Всегда возвращаем голосовой канал, игнорируя содержание."""
            return "voice"

    engine = ProactiveEngine(
        DummyPolicy(),
        response_timeout_sec=1,
    )

    # Имитируем ожидание ответа на подсказку.
    engine._await_response(1, "выпей воды")

    called = {"skill": False, "cmd": False}
    monkeypatch.setattr(
        cp,
        "handle_utterance",
        lambda cmd: (called.__setitem__("skill", True), False)[1],
    )
    monkeypatch.setattr(
        cp,
        "execute_cmd",
        lambda cmd, voice: (called.__setitem__("cmd", True), False)[1],
    )

    feedback: list = []
    monkeypatch.setattr(
        cp,
        "add_suggestion_feedback",
        lambda sid, text, acc: feedback.append((sid, text, acc)),
    )

    events: list = []
    from core import events as core_events

    core_events.subscribe("suggestion.response", lambda e: events.append(e))

    async def run():
        assert await cp.va_respond("джарвис ок") is True

    asyncio.run(run())

    assert called["skill"] is False
    assert called["cmd"] is False
    assert feedback == [(1, "ок", True)]
    assert events and events[0].attrs["suggestion_id"] == 1

