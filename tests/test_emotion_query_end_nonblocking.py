import asyncio
import sys
import types

import core.events as core_events
from core.events import Event
import pytest


# Заглушки зависимостей, требующих нативных библиотек.
sys.modules.setdefault("sounddevice", types.SimpleNamespace())
sys.modules.setdefault(
    "working_tts",
    types.SimpleNamespace(stop_speaking=lambda: None, speak_async=lambda *a, **k: None),
)

from emotion.manager import EmotionManager
from emotion.state import Emotion


@pytest.fixture(autouse=True)
def clean_bus():
    """Очистка подписчиков перед каждым тестом."""
    core_events._subscribers.clear()
    core_events._global_subscribers.clear()


def test_on_query_ended_non_blocking(monkeypatch):
    """Обработчик не должен блокировать event loop."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    timer_started = False

    class AsyncTimer:
        """Таймер, использующий event loop вместо sleep."""

        def __init__(self, delay, func, *a, **kw):
            self._func = lambda: func(*a, **kw)

        def start(self):
            nonlocal timer_started
            timer_started = True
            loop.call_soon(self._func)

        def cancel(self):  # pragma: no cover - для совместимости API
            pass

    monkeypatch.setattr("emotion.manager.Timer", AsyncTimer)

    mgr = EmotionManager()
    mgr._prev_emotion = Emotion.NEUTRAL
    # Отключаем реальный таймер простоя, чтобы не зациклить тест
    monkeypatch.setattr(mgr, "_reset_idle_timer", lambda: None)

    marker_run = False

    def marker():
        nonlocal marker_run
        marker_run = True

    async def runner():
        loop.call_soon(marker)
        start = loop.time()
        mgr._on_query_ended(Event(kind="user_query_ended"))
        await asyncio.sleep(0)
        return loop.time() - start

    elapsed = loop.run_until_complete(runner())
    loop.close()

    assert marker_run, "event loop was blocked by on_query_ended"
    assert timer_started, "Timer was not started"
    assert elapsed < 0.05, f"handler blocked loop for {elapsed:.3f}s"

