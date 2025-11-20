"""Тесты подтверждают отключение эмоций в TTS."""
from __future__ import annotations

import asyncio
import contextlib
import sys
import types

import pytest

from core.request_source import reset_request_source, set_request_source


async def _dummy_speak_async(text: str, *, loop=None) -> None:
    """Заглушка для ``working_tts`` в тестах."""
    return None


def _load_voice(monkeypatch):
    """Импортировать ``notifiers.voice`` с подменённым ``working_tts``."""

    dummy_module = types.SimpleNamespace(speak_async=_dummy_speak_async)
    monkeypatch.setitem(sys.modules, "working_tts", dummy_module)
    monkeypatch.delitem(sys.modules, "notifiers.voice", raising=False)
    import notifiers.voice as voice

    return voice


def test_voice_worker_passes_plain_text(monkeypatch):
    """Очередь должна передавать в ``speak_async`` только текст без параметров."""

    voice = _load_voice(monkeypatch)
    captured: dict[str, str] = {}

    async def fake_speak_async(text: str, *, loop=None) -> None:
        captured["text"] = text

    async def run_test() -> None:
        monkeypatch.setattr(voice, "speak_async", fake_speak_async)
        monkeypatch.setattr(voice, "set_metric", lambda name, value: None)

        token = set_request_source("voice")
        try:
            voice._queue = asyncio.Queue()
            if voice._worker_task is not None:
                voice._worker_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await voice._worker_task
                voice._worker_task = None

            voice.send("привет")
            await asyncio.wait_for(voice._queue.join(), timeout=1.0)
        finally:
            reset_request_source(token)

        assert captured == {"text": "привет"}

        assert voice._worker_task is not None
        voice._worker_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await voice._worker_task
        voice._worker_task = None

    asyncio.run(run_test())


def test_voice_send_rejects_emotion_kwargs(monkeypatch):
    """Попытка передать эмоцию должна завершаться ``TypeError``."""

    voice = _load_voice(monkeypatch)

    with pytest.raises(TypeError):
        voice.send("hi", emotion="sad")  # type: ignore[call-arg]
