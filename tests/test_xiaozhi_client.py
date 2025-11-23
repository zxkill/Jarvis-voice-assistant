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


def test_ensure_efuse_generates_persistent_values(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Efuse должен генерироваться детерминированно и храниться в конфиге."""

    cfg = XiaozhiConfigManager(tmp_path / "xiaozhi.json")

    # Жёстко фиксируем hmac, чтобы тест оставался предсказуемым.
    monkeypatch.setattr("secrets.token_hex", lambda n=32: "ab" * n)

    efuse = cfg.ensure_efuse(
        mac="aa:bb:cc:dd:ee:ff",
        machine_id="machine-1234",
        system="Linux",
        hostname="jarvis",
    )

    assert efuse["serial_number"].startswith("SN-machine")
    assert efuse["hmac_key"] == "ab" * 32
    # Повторный вызов не должен менять значения.
    efuse2 = cfg.ensure_efuse(
        mac="aa:bb:cc:dd:ee:ff",
        machine_id="machine-1234",
        system="Linux",
        hostname="jarvis",
    )
    assert efuse == efuse2


def test_device_id_prefers_mac(tmp_path: Path) -> None:
    """Device_id должен совпадать с MAC, как в оригинальном клиенте."""

    cfg = XiaozhiConfigManager(tmp_path / "xiaozhi.json")

    class DummyProfile:
        def __init__(self) -> None:
            self.system = "Linux"
            self.hostname = "jarvis"
            self.hardware_hash = "hash"
            self.mac_address = "11:22:33:44:55:66"
            self.machine_id = "machine"
            self.ip_address = "127.0.0.1"

    class DummyDeviceInfo:
        def profile(self) -> DummyProfile:
            return DummyProfile()

        def as_payload(self) -> dict[str, str]:
            return {
                "system": "Linux",
                "hostname": "jarvis",
                "hardware_hash": "hash",
                "mac": "11:22:33:44:55:66",
                "machine_id": "machine",
                "ip": "127.0.0.1",
            }

    client = XiaozhiClient(cfg)
    client.device_info = DummyDeviceInfo()  # type: ignore[assignment]

    assert client._ensure_device_id() == "11:22:33:44:55:66"
    assert cfg.get("device_id") == "11:22:33:44:55:66"


@pytest.mark.asyncio
async def test_ensure_remote_config_updates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Убеждаемся, что OTA ответ сохраняется в конфиг."""

    cfg = XiaozhiConfigManager(tmp_path / "xiaozhi.json")
    client = XiaozhiClient(cfg)

    payload = {
        "websocket": {"url": "ws://example", "token": "secret-token"},
        "activation": {"code": "1234", "message": "msg", "challenge": "c"},
        "mqtt": {"endpoint": "mqtt.example", "client_id": "cid"},
    }

    captured_headers: dict[str, Any] = {}

    def fake_post(*_: Any, **__: Any) -> DummyResponse:
        captured_headers.update(__.get("headers", {}))
        return DummyResponse(payload)

    monkeypatch.setattr("requests.post", fake_post)

    data = await client.ensure_remote_config()

    assert data["websocket_url"] == "ws://example"
    assert data["websocket_token"] == "secret-token"
    assert data["network"]["websocket"]["url"] == "ws://example"
    assert data["network"]["mqtt"]["endpoint"] == "mqtt.example"
    assert data["activation"]["code"] == "1234"
    assert captured_headers.get("Activation-Version") == "v2"
    assert captured_headers.get("Accept-Language") == cfg.get("accept_language")
    assert data["efuse"]["mac_address"]


@pytest.mark.asyncio
async def test_ensure_remote_config_respects_activation_version_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Для протокола v1 заголовок Activation-Version не добавляется."""

    cfg = XiaozhiConfigManager(tmp_path / "xiaozhi.json")
    cfg.update(activation_version="v1", app_version="3.1.4")
    client = XiaozhiClient(cfg)

    payload = {"websocket": {"url": "ws://example", "token": "secret-token"}}
    captured_headers: dict[str, Any] = {}

    def fake_post(*_: Any, **__: Any) -> DummyResponse:
        captured_headers.update(__.get("headers", {}))
        return DummyResponse(payload)

    monkeypatch.setattr("requests.post", fake_post)

    await client.ensure_remote_config()

    assert "Activation-Version" not in captured_headers
    assert captured_headers.get("User-Agent") == "linux/jarvis-3.1.4"


@pytest.mark.asyncio
async def test_ensure_remote_config_raises_on_bad_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """При ответе не 200 поднимается понятная ошибка с телом ответа."""

    cfg = XiaozhiConfigManager(tmp_path / "xiaozhi.json")
    client = XiaozhiClient(cfg)

    def fake_post(*_: Any, **__: Any) -> DummyResponse:
        return DummyResponse({"error": "bad request"}, status_code=400)

    monkeypatch.setattr("requests.post", fake_post)

    with pytest.raises(RuntimeError) as err:
        await client.ensure_remote_config()

    assert "OTA status 400" in str(err.value)
    assert "bad request" in str(err.value)


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
