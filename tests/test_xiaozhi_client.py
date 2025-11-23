"""Тесты для интеграции Xiaozhi."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from core.xiaozhi_client import XiaozhiClient
from core.xiaozhi_config import XiaozhiConfigManager


class DummyResponse:
    """Ответ, имитирующий requests.Response."""

    def __init__(self, payload: dict[str, Any], status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload)

    def json(self) -> dict[str, Any]:
        return self._payload


class DummyWebSocket:
    """Минимальная заглушка WebSocket для тестирования."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self._queue: asyncio.Queue[str | None] = asyncio.Queue()
        self.closed = False

    async def send(self, message: str) -> None:
        self.sent.append(message)

    def feed(self, message: str | None) -> None:
        """Помещает сообщение в очередь ответа."""

        self._queue.put_nowait(message)

    def __aiter__(self) -> "DummyWebSocket":
        return self

    async def __anext__(self) -> str:
        item = await self._queue.get()
        if item is None:
            raise StopAsyncIteration
        return item


@pytest.mark.asyncio
async def test_ensure_remote_config_updates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Убеждаемся, что OTA ответ сохраняется в конфиг."""

    cfg = XiaozhiConfigManager(tmp_path / "xiaozhi.json")
    client = XiaozhiClient(cfg)

    payload = {
        "websocket": {"url": "ws://example", "token": "secret-token"},
        "activation": {"code": "1234", "message": "msg", "challenge": "c"},
    }

    def fake_post(*_: Any, **__: Any) -> DummyResponse:
        return DummyResponse(payload)

    monkeypatch.setattr("requests.post", fake_post)

    data = await client.ensure_remote_config()

    assert data["websocket_url"] == "ws://example"
    assert data["websocket_token"] == "secret-token"
    assert data["activation"]["code"] == "1234"


@pytest.mark.asyncio
async def test_ask_text_uses_websocket(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Клиент отправляет hello и читает текстовый ответ."""

    cfg = XiaozhiConfigManager(tmp_path / "xiaozhi.json")
    cfg.update(
        websocket_url="ws://example",
        websocket_token="secret-token",
        device_id="dev",
        client_id="cli",
    )

    ws = DummyWebSocket()
    ws.feed(json.dumps({"text": "привет от Xiaozhi"}))
    ws.feed(None)

    async def fake_connect(*_: Any, **__: Any) -> DummyWebSocket:
        return ws

    monkeypatch.setattr("websockets.connect", fake_connect)

    client = XiaozhiClient(cfg)
    reply = await client.ask_text("hi", trace_id="trace", timeout=1)

    assert reply == "привет от Xiaozhi"
    assert any("hello" in sent for sent in ws.sent)
    assert any("hi" in sent for sent in ws.sent)
