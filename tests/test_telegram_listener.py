import sys
from types import SimpleNamespace

import pytest
import requests


class DummyResp:
    def __init__(self, data, status_code=200):
        self._data = data
        self.status_code = status_code
        self.text = ""

    def json(self):
        return self._data


def _load_listener(monkeypatch):
    cfg = SimpleNamespace(
        telegram=SimpleNamespace(token="TOKEN"),
        user=SimpleNamespace(telegram_user_id=123),
    )
    dummy_cmd = SimpleNamespace()
    async def _dummy_va(_):
        return None
    dummy_cmd.va_respond = _dummy_va
    monkeypatch.setitem(sys.modules, "app.command_processing", dummy_cmd)
    monkeypatch.setattr("core.config.load_config", lambda: cfg)
    monkeypatch.delitem(sys.modules, "notifiers.telegram_listener", raising=False)
    import notifiers.telegram_listener as tl
    return tl


def test_listener_processes_and_updates_offset(monkeypatch):
    tl = _load_listener(monkeypatch)
    calls = []

    async def fake_va(text):
        calls.append(text)

    metrics = {"count": 0}

    def fake_inc(name):
        metrics["count"] += 1

    responses = [
        DummyResp(
            {
                "ok": True,
                "result": [
                    {
                        "update_id": 5,
                        "message": {"chat": {"id": 123}, "text": "cmd"},
                    }
                ],
            }
        ),
        DummyResp({"ok": True, "result": []}),
    ]
    offsets = []

    def fake_get(url, params, timeout):
        offsets.append(params["offset"])
        return responses.pop(0)

    monkeypatch.setattr(tl, "va_respond", fake_va)
    monkeypatch.setattr(tl, "inc_metric", fake_inc)
    monkeypatch.setattr(tl.requests, "get", fake_get)

    tl.listen(max_iterations=2)

    assert calls == ["cmd"]
    assert metrics["count"] == 1
    assert offsets == [0, 6]


def test_listener_ignores_foreign_chat(monkeypatch):
    tl = _load_listener(monkeypatch)
    calls = []

    async def fake_va(text):
        calls.append(text)

    metrics = {"count": 0}

    def fake_inc(name):
        metrics["count"] += 1

    def fake_get(url, params, timeout):
        return DummyResp(
            {
                "ok": True,
                "result": [
                    {
                        "update_id": 1,
                        "message": {"chat": {"id": 999}, "text": "no"},
                    }
                ],
            }
        )

    monkeypatch.setattr(tl, "va_respond", fake_va)
    monkeypatch.setattr(tl, "inc_metric", fake_inc)
    monkeypatch.setattr(tl.requests, "get", fake_get)

    tl.listen(max_iterations=1)

    assert calls == []
    assert metrics["count"] == 0


def test_listener_handles_api_error(monkeypatch):
    tl = _load_listener(monkeypatch)
    calls = []

    async def fake_va(text):
        calls.append(text)

    def fake_get(url, params, timeout):
        return DummyResp({"ok": False, "result": []}, status_code=500)

    monkeypatch.setattr(tl, "va_respond", fake_va)
    monkeypatch.setattr(tl.requests, "get", fake_get)

    tl.listen(max_iterations=1)

    assert calls == []


def test_listener_retries_on_network_error(monkeypatch):
    tl = _load_listener(monkeypatch)
    calls = []

    async def fake_va(text):
        calls.append(text)

    metrics = {"count": 0}

    def fake_inc(name):
        metrics["count"] += 1

    def fake_sleep(sec):
        pass

    call_counter = {"n": 0}

    def fake_get(url, params, timeout):
        call_counter["n"] += 1
        if call_counter["n"] == 1:
            raise requests.RequestException("boom")
        return DummyResp(
            {
                "ok": True,
                "result": [
                    {
                        "update_id": 7,
                        "message": {"chat": {"id": 123}, "text": "hi"},
                    }
                ],
            }
        )

    monkeypatch.setattr(tl, "va_respond", fake_va)
    monkeypatch.setattr(tl, "inc_metric", fake_inc)
    monkeypatch.setattr(tl.requests, "get", fake_get)
    monkeypatch.setattr(tl.time, "sleep", fake_sleep)

    tl.listen(max_iterations=2)

    assert calls == ["hi"]
    assert metrics["count"] == 1
    assert call_counter["n"] == 2
