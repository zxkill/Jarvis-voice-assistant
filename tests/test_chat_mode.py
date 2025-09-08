"""Проверка режима общения с передачей непонятных запросов в LLM."""

import sys
import asyncio
from types import SimpleNamespace

import pytest


def _load_cp(monkeypatch):
    """Импортировать ``command_processing`` с подменой зависимостей."""

    spoken: list[str] = []
    summaries: list[dict] = []

    dummy_skills = SimpleNamespace(handle_utterance=lambda cmd: False)
    dummy_nlp = SimpleNamespace(normalize=lambda x: x)

    async def fake_speak(text: str, *a, **k) -> None:
        spoken.append(text)

    dummy_tts = SimpleNamespace(speak_async=fake_speak)

    dummy_llm = SimpleNamespace(
        think=lambda text, trace_id=None: f"ответ:{text}",
        summarise=lambda text, labels=None: "summary",
    )

    dummy_daily = SimpleNamespace(add=lambda item: summaries.append(item))

    monkeypatch.setitem(sys.modules, "jarvis_skills", dummy_skills)
    monkeypatch.setitem(sys.modules, "core.nlp", dummy_nlp)
    monkeypatch.setitem(sys.modules, "working_tts", dummy_tts)
    monkeypatch.setitem(sys.modules, "context.daily_memory", dummy_daily)

    monkeypatch.delitem(sys.modules, "app.command_processing", raising=False)
    import app.command_processing as cp

    monkeypatch.setattr(cp, "llm_engine", dummy_llm)
    return cp, spoken, summaries


def test_conversation_flow(monkeypatch):
    """После неизвестной команды активируется LLM-диалог."""

    cp, spoken, summaries = _load_cp(monkeypatch)

    async def run() -> None:
        await cp.va_respond("джарвис расскажи анекдот")
        assert cp.CHAT_MODE_ACTIVE is True
        assert cp.CHAT_HISTORY[0][0].endswith("анекдот")
        assert cp.CHAT_HISTORY[0][1].endswith("анекдот")

        await cp.va_respond("и ещё один")
        assert cp.CHAT_HISTORY[-1] == ("и ещё один", "ответ:и ещё один")

        await cp.va_respond("хватит болтать")

    asyncio.run(run())
    assert cp.CHAT_MODE_ACTIVE is False
    # Последняя озвученная фраза — уведомление о завершении режима общения
    assert spoken[-1] == "Режим общения завершён"
