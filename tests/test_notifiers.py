import asyncio
import contextlib
import importlib
import sys
from types import SimpleNamespace

import pytest
import requests


class DummyResp:
    def __init__(self, status_code=200, ok=True, text=""):
        self.status_code = status_code
        self._ok = ok
        self.text = text

    def json(self):
        return {"ok": self._ok}


def _load_telegram(monkeypatch):
    cfg = SimpleNamespace(
        telegram=SimpleNamespace(token="TOKEN"),
        user=SimpleNamespace(telegram_user_id=123),
    )
    monkeypatch.setattr("core.config.load_config", lambda: cfg)
    monkeypatch.delitem(sys.modules, "notifiers.telegram", raising=False)
    import notifiers.telegram as telegram
    return telegram


def _load_voice(monkeypatch):
    async def dummy_speak_async(text: str):
        pass

    dummy_module = SimpleNamespace(speak_async=dummy_speak_async)
    monkeypatch.setitem(sys.modules, "working_tts", dummy_module)
    monkeypatch.delitem(sys.modules, "notifiers.voice", raising=False)
    import notifiers.voice as voice
    return voice


def test_telegram_notifier_send_success(monkeypatch):
    telegram = _load_telegram(monkeypatch)
    sent = {}

    def fake_post(url, json, timeout):
        sent["url"] = url
        sent["json"] = json
        return DummyResp()

    metric_calls = {"count": 0}

    def fake_inc_metric(name):
        metric_calls["count"] += 1

    monkeypatch.setattr(telegram.requests, "post", fake_post)
    monkeypatch.setattr(telegram, "inc_metric", fake_inc_metric)

    notifier = telegram.TelegramNotifier("TOKEN", 123)
    notifier.send("hello")

    assert metric_calls["count"] == 0
    assert sent["url"].endswith("/sendMessage")
    assert sent["json"] == {"chat_id": 123, "text": "hello"}


def test_telegram_notifier_send_failure(monkeypatch):
    telegram = _load_telegram(monkeypatch)

    def fake_post(url, json, timeout):
        raise requests.RequestException("boom")

    metric_calls = {"count": 0}

    def fake_inc_metric(name):
        metric_calls["count"] += 1

    monkeypatch.setattr(telegram.requests, "post", fake_post)
    monkeypatch.setattr(telegram, "inc_metric", fake_inc_metric)

    notifier = telegram.TelegramNotifier("TOKEN", 123)
    notifier.send("hello")

    assert metric_calls["count"] == 1


def test_telegram_send_wrapper(monkeypatch):
    telegram = _load_telegram(monkeypatch)
    sent = []

    class DummyNotifier:
        def send(self, text):
            sent.append(text)

    monkeypatch.setattr(telegram, "_notifier", DummyNotifier())
    telegram.send("hi")

    assert sent == ["hi"]


def test_voice_send_processes_queue(monkeypatch):
    voice = _load_voice(monkeypatch)
    spoken = []

    async def fake_speak_async(text):
        spoken.append(text)

    async def run_test():
        monkeypatch.setattr(voice, "speak_async", fake_speak_async)
        monkeypatch.setattr(voice, "set_metric", lambda name, value: None)

        # reset queue and worker state
        voice._queue = asyncio.Queue()
        if voice._worker_task is not None:
            voice._worker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await voice._worker_task
            voice._worker_task = None

        voice.send("test")
        assert voice._worker_task is not None

        await asyncio.wait_for(voice._queue.join(), timeout=1)
        assert spoken == ["test"]

        # cancel worker task to clean up
        voice._worker_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await voice._worker_task
        voice._worker_task = None

    asyncio.run(run_test())
