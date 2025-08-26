import asyncio
import contextlib
import sys
from types import SimpleNamespace

import pytest


async def _dummy_speak_async(text: str, *, pitch=None, speed=None, emotion=None, loop=None):
    """Заглушка для модуля ``working_tts``.

    В тестах нас интересует только факт передачи параметров,
    поэтому функция ничего не воспроизводит."""
    pass


def _load_voice(monkeypatch):
    """Загрузить модуль ``notifiers.voice`` с подменённым ``working_tts``."""
    dummy_module = SimpleNamespace(speak_async=_dummy_speak_async)
    monkeypatch.setitem(sys.modules, "working_tts", dummy_module)
    monkeypatch.delitem(sys.modules, "notifiers.voice", raising=False)
    import notifiers.voice as voice
    return voice


def test_voice_passes_emotion_params(monkeypatch):
    voice = _load_voice(monkeypatch)
    captured = {}

    async def fake_speak_async(text, *, pitch=None, speed=None, emotion=None, loop=None):
        captured.update({
            "text": text,
            "pitch": pitch,
            "speed": speed,
            "emotion": emotion,
        })

    async def run_test():
        monkeypatch.setattr(voice, "speak_async", fake_speak_async)
        monkeypatch.setattr(voice, "set_metric", lambda name, value: None)

        # Сбрасываем очередь и воркер перед тестом
        voice._queue = asyncio.Queue()
        if voice._worker_task is not None:
            voice._worker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await voice._worker_task
            voice._worker_task = None

        voice.send("hi", pitch=1.3, speed=1.1, emotion="happy")
        await asyncio.wait_for(voice._queue.join(), timeout=1)

        assert captured == {
            "text": "hi",
            "pitch": 1.3,
            "speed": 1.1,
            "emotion": "happy",
        }

        # Останавливаем воркер, чтобы не мешал другим тестам
        voice._worker_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await voice._worker_task
        voice._worker_task = None

    asyncio.run(run_test())
