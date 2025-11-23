"""Тесты для интеграции Xiaozhi."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from websockets.exceptions import ConnectionClosed, InvalidStatusCode
from websockets.frames import Close

from core.xiaozhi_client import XiaozhiClient
from core.xiaozhi_config import XiaozhiConfigManager
from core.xiaozhi_device import normalize_mac


@pytest.fixture(autouse=True)
def cleanup_background_tasks(event_loop: asyncio.AbstractEventLoop) -> None:
    """Автоматически гасит фоновые задачи активации после тестов.

    В противном случае долгая корутина `_activate_device` может остаться висеть
    после завершения теста и выдавать предупреждение о разрушенной задаче.
    """

    yield

    pending = [
        task
        for task in asyncio.all_tasks(event_loop)
        if not task.done() and getattr(task.get_coro(), "__name__", "") == "_activate_device"
    ]
    for task in pending:
        task.cancel()
    if pending:
        event_loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))


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
        self.recv_calls: list[str | None] = []

    async def send(self, message: str) -> None:
        self.sent.append(message)

    async def feed(self, message: str | None) -> None:
        """Помещает сообщение в очередь ответа."""

        self._queue.put_nowait(message)

    async def recv(self) -> str:
        item = await self._queue.get()
        self.recv_calls.append(item)
        if item is None:
            raise ConnectionClosed(Close(1000, "closed"), Close(1000, "closed"))
        return item

    def __aiter__(self) -> "DummyWebSocket":
        return self

    async def __anext__(self) -> str:
        try:
            return await self.recv()
        except ConnectionClosed:
            raise StopAsyncIteration


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


def test_resolve_mac_prefers_configured_efuse(tmp_path: Path) -> None:
    """Если MAC есть в efuse, он используется и нормализуется."""

    cfg = XiaozhiConfigManager(tmp_path / "xiaozhi.json")
    cfg.update(efuse={"mac_address": "7a-46-5c-d2-3c-2b"})

    client = XiaozhiClient(cfg)

    assert client._resolve_mac() == "7A:46:5C:D2:3C:2B"


def test_normalize_mac_supports_various_formats() -> None:
    """Нормализация должна принимать разные разделители и регистр."""

    assert normalize_mac("7a:46:5c:d2:3c:2b") == "7A:46:5C:D2:3C:2B"
    assert normalize_mac("7A-46-5C-D2-3C-2B") == "7A:46:5C:D2:3C:2B"
    assert normalize_mac("7A46.5CD2.3C2B") == "7A:46:5C:D2:3C:2B"

    with pytest.raises(ValueError):
        normalize_mac("123")
    with pytest.raises(ValueError):
        normalize_mac("zz:zz:zz:zz:zz:zz")


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
    captured_payload: dict[str, Any] = {}

    def fake_post(*_: Any, **__: Any) -> DummyResponse:
        captured_headers.update(__.get("headers", {}))
        captured_payload.update(__.get("json", {}))
        return DummyResponse(payload)

    monkeypatch.setattr("requests.post", fake_post)

    data = await client.ensure_remote_config()

    assert data["websocket_url"] == "ws://example"
    assert data["websocket_token"] == "secret-token"
    assert data["network"]["websocket"]["url"] == "ws://example"
    assert data["network"]["mqtt"]["endpoint"] == "mqtt.example"
    assert data["activation"]["code"] == "1234"
    assert captured_headers.get("Activation-Version") == cfg.get("app_version")
    assert captured_headers.get("Accept-Language") == cfg.get("accept_language")
    assert data["efuse"]["mac_address"]
    assert captured_payload.get("application", {}).get("elf_sha256") == cfg.get("efuse")["hmac_key"]


@pytest.mark.asyncio
async def test_ensure_remote_config_forces_refresh_until_activated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Даже при наличии кэша до активации нужно повторно сходить в OTA."""

    cfg = XiaozhiConfigManager(tmp_path / "xiaozhi.json")
    # Симулируем сохранённый ответ до ввода кода: токены есть, но активации нет.
    cfg.update(
        websocket_url="ws://cached",
        websocket_token="cached-token",
        efuse={"activation_status": False},
        activation={"code": "111222"},
    )

    client = XiaozhiClient(cfg)

    called = 0

    def fake_post(*_: Any, **__: Any) -> DummyResponse:
        nonlocal called
        called += 1
        payload = {
            "websocket": {"url": "ws://fresh", "token": "fresh-token"},
            "activation": {"activation_status": True},
        }
        return DummyResponse(payload)

    monkeypatch.setattr("requests.post", fake_post)

    data = await client.ensure_remote_config()

    assert called == 1  # Кэш не использован, сходили за новым токеном.
    assert data["websocket_url"] == "ws://fresh"
    assert data["efuse"]["activation_status"] is True


@pytest.mark.asyncio
async def test_ensure_remote_config_uses_cache_after_activation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """После подтверждённой активации кэш повторно не дёргает OTA."""

    cfg = XiaozhiConfigManager(tmp_path / "xiaozhi.json")
    cfg.update(
        websocket_url="ws://cached", websocket_token="cached-token", efuse={"activation_status": True}
    )

    client = XiaozhiClient(cfg)

    called = 0

    def fake_post(*_: Any, **__: Any) -> DummyResponse:  # pragma: no cover - не должен вызываться
        nonlocal called
        called += 1
        return DummyResponse({"websocket": {"url": "ws://fresh", "token": "fresh-token"}})

    monkeypatch.setattr("requests.post", fake_post)

    data = await client.ensure_remote_config()

    assert called == 0  # Использовали кэш, не стучались в сеть.
    assert data["websocket_url"] == "ws://cached"


@pytest.mark.asyncio
async def test_activation_code_is_returned_to_user_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Код активации должен один раз вернуться пользователю вместо диалога."""

    cfg = XiaozhiConfigManager(tmp_path / "xiaozhi.json")
    client = XiaozhiClient(cfg)

    payload = {
        "websocket": {"url": "ws://example", "token": "secret-token"},
        "activation": {"code": "5678", "message": "Введите его на xiaozhi.me"},
    }

    def fake_post(*_: Any, **__: Any) -> DummyResponse:
        return DummyResponse(payload)

    called_ws = False

    async def fake_connect(*_: Any, **__: Any) -> DummyWebSocket:  # pragma: no cover - на первом вызове не должен сработать
        nonlocal called_ws
        called_ws = True
        return DummyWebSocket()

    monkeypatch.setattr("requests.post", fake_post)
    monkeypatch.setattr("websockets.connect", fake_connect)

    reply = await client.ask_text("hi", trace_id="trace", timeout=1)

    assert "5 6 7 8" in (reply or "")
    assert called_ws is False  # код отдали в чат без открытия WebSocket
    assert cfg.get("activation")["last_notified_code"] == "5678"

    # Повторный вызов должен идти в WebSocket, так как код уже доставлен.
    ws = DummyWebSocket()
    await ws.feed(json.dumps({"type": "hello"}))
    await ws.feed(json.dumps({"text": "ответ после активации"}))
    await ws.feed(None)

    async def fake_connect_second(*_: Any, **__: Any) -> DummyWebSocket:
        return ws

    monkeypatch.setattr("websockets.connect", fake_connect_second)

    reply2 = await client.ask_text("hi", trace_id="trace", timeout=1)

    assert reply2 == "ответ после активации"


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
async def test_start_activation_if_needed_triggers_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Активация запускается в фоне, когда есть challenge и код."""

    cfg = XiaozhiConfigManager(tmp_path / "xiaozhi.json")
    cfg.update(
        efuse={"serial_number": "SN-1", "hmac_key": "0" * 64, "activation_status": False},
        activation={},
    )
    client = XiaozhiClient(cfg)

    started: list[str] = []

    async def fake_activate(**kwargs: Any) -> bool:
        started.append(kwargs["challenge"])
        return True

    monkeypatch.setattr(client, "_activate_device", fake_activate)

    await client._start_activation_if_needed(
        activation={"challenge": "abc", "code": "123456"},
        efuse=cfg.get("efuse"),
        ota_url="https://api.tenclass.net/xiaozhi/ota/",
        device_id="AA:BB:CC:DD:EE:FF",
        client_id="client",
    )

    assert client._activation_task is not None
    await client._activation_task

    # Повторный вызов не создаёт новую задачу, пока старая не завершилась.
    await client._start_activation_if_needed(
        activation={"challenge": "abc", "code": "123456"},
        efuse=cfg.get("efuse"),
        ota_url="https://api.tenclass.net/xiaozhi/ota/",
        device_id="AA:BB:CC:DD:EE:FF",
        client_id="client",
    )

    assert started == ["abc"]


@pytest.mark.asyncio
async def test_activate_device_posts_hmac_and_sets_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /activate формируется по образцу py-xiaozhi и фиксирует активацию."""

    cfg = XiaozhiConfigManager(tmp_path / "xiaozhi.json")
    cfg.update(
        efuse={"serial_number": "SN-XYZ", "hmac_key": "a" * 64, "activation_status": False},
        websocket_url="ws://cached",
        websocket_token="token",
    )
    client = XiaozhiClient(cfg)

    calls: list[dict[str, Any]] = []

    def fake_post(*_: Any, **kwargs: Any) -> DummyResponse:
        calls.append({"headers": kwargs.get("headers", {}), "json": kwargs.get("json", {})})
        # Первый ответ 202 имитирует ожидание ввода кода, второй — успешную активацию.
        status = 200 if len(calls) > 1 else 202
        return DummyResponse({}, status_code=status)

    monkeypatch.setattr("requests.post", fake_post)

    result = await client._activate_device(
        activate_url="https://api.tenclass.net/xiaozhi/ota/activate",
        challenge="challenge-1",
        serial_number="SN-XYZ",
        hmac_key="a" * 64,
        device_id="AA:BB:CC:DD:EE:FF",
        client_id="client-1",
        code="123456",
        max_attempts=2,
        retry_interval=0,
    )

    assert result is True
    assert calls[0]["headers"]["Activation-Version"] == "2"
    assert calls[0]["json"]["Payload"]["serial_number"] == "SN-XYZ"
    assert cfg.get("efuse")["activation_status"] is True
    assert cfg.get("activation")["code"] is None
    assert cfg.get("websocket_url") is None


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
async def test_ensure_remote_config_fails_fast_on_invalid_mac(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """При некорректном MAC запрос OTA даже не отправляется."""

    cfg = XiaozhiConfigManager(tmp_path / "xiaozhi.json")
    cfg.update(device_id="invalid-mac")

    class DummyProfile:
        def __init__(self) -> None:
            self.system = "Linux"
            self.hostname = "jarvis"
            self.hardware_hash = "hash"
            self.mac_address = "invalid-mac"
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
                "mac": "invalid-mac",
                "machine_id": "machine",
                "ip": "127.0.0.1",
            }

    client = XiaozhiClient(cfg)
    client.device_info = DummyDeviceInfo()  # type: ignore[assignment]

    called = False

    def fake_post(*_: Any, **__: Any) -> DummyResponse:  # pragma: no cover - защита от случайного вызова
        nonlocal called
        called = True
        return DummyResponse({})

    monkeypatch.setattr("requests.post", fake_post)

    with pytest.raises(RuntimeError) as err:
        await client.ensure_remote_config()

    assert "не удалось определить MAC" in str(err.value)
    assert called is False


@pytest.mark.asyncio
async def test_marks_activated_when_code_disappears(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Если код был и пропал, ставим activation_status=True для кэша."""

    cfg = XiaozhiConfigManager(tmp_path / "xiaozhi.json")
    cfg.update(activation={"code": "999111"}, efuse={"activation_status": False})

    payload = {"websocket": {"url": "ws://example", "token": "secret-token"}, "activation": {}}

    def fake_post(*_: Any, **__: Any) -> DummyResponse:
        return DummyResponse(payload)

    monkeypatch.setattr("requests.post", fake_post)

    client = XiaozhiClient(cfg)
    await client.ensure_remote_config()

    assert cfg.get("efuse")["activation_status"] is True


@pytest.mark.asyncio
async def test_ask_text_uses_websocket(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Клиент отправляет hello и читает текстовый ответ."""

    cfg = XiaozhiConfigManager(tmp_path / "xiaozhi.json")
    cfg.update(
        websocket_url="ws://example",
        websocket_token="secret-token",
        device_id="AA:BB:CC:DD:EE:FF",
        client_id="cli",
    )

    ws = DummyWebSocket()
    await ws.feed(json.dumps({"type": "hello"}))
    await ws.feed(json.dumps({"text": "привет от Xiaozhi"}))
    await ws.feed(None)

    async def fake_connect(*_: Any, **__: Any) -> DummyWebSocket:
        return ws

    monkeypatch.setattr("websockets.connect", fake_connect)

    client = XiaozhiClient(cfg)
    reply = await client.ask_text("hi", trace_id="trace", timeout=1)

    assert reply == "привет от Xiaozhi"
    # hello уходит первым, за ним listen/detect с текстом, как в py-xiaozhi
    assert any("hello" in sent for sent in ws.sent)
    sent_payloads = [json.loads(raw) for raw in ws.sent if "listen" in raw]
    assert sent_payloads
    assert sent_payloads[-1]["state"] == "detect"
    assert sent_payloads[-1]["text"] == "hi"
    assert sent_payloads[-1]["session_id"]


@pytest.mark.asyncio
async def test_ask_text_sends_trace_id_and_detect(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Поле trace_id должно попадать в listen/detect, как в py-xiaozhi."""

    cfg = XiaozhiConfigManager(tmp_path / "xiaozhi.json")
    cfg.update(
        websocket_url="ws://example",
        websocket_token="secret-token",
        device_id="AA:BB:CC:DD:EE:FF",
        client_id="cli",
    )

    ws = DummyWebSocket()
    await ws.feed(json.dumps({"type": "hello"}))
    await ws.feed(json.dumps({"text": "ответ"}))
    await ws.feed(None)

    async def fake_connect(*_: Any, **__: Any) -> DummyWebSocket:
        return ws

    monkeypatch.setattr("websockets.connect", fake_connect)

    client = XiaozhiClient(cfg)
    reply = await client.ask_text("ping", trace_id="trace-id", timeout=1)

    assert reply == "ответ"
    sent_payloads = [json.loads(raw) for raw in ws.sent if "listen" in raw]
    assert sent_payloads[-1]["trace_id"] == "trace-id"
    assert sent_payloads[-1]["state"] == "detect"


@pytest.mark.asyncio
async def test_waits_for_server_hello_before_listen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Клиент ожидает hello от сервера и только потом шлёт listen/detect."""

    cfg = XiaozhiConfigManager(tmp_path / "xiaozhi.json")
    cfg.update(
        websocket_url="ws://example",
        websocket_token="secret-token",
        device_id="AA:BB:CC:DD:EE:FF",
        client_id="cli",
    )

    ws = DummyWebSocket()
    # Сначала придёт серверный hello, затем текстовый ответ
    await ws.feed(json.dumps({"type": "hello", "transport": "websocket"}))
    await ws.feed(json.dumps({"text": "ответ после hello"}))
    await ws.feed(None)

    async def fake_connect(*_: Any, **__: Any) -> DummyWebSocket:
        return ws

    monkeypatch.setattr("websockets.connect", fake_connect)

    client = XiaozhiClient(cfg)
    reply = await client.ask_text("hi", timeout=1)

    assert reply == "ответ после hello"
    # Проверяем, что серверный hello действительно считан до listen/detect
    assert ws.recv_calls and json.loads(ws.recv_calls[0])["type"] == "hello"
    sent_payloads = [json.loads(raw) for raw in ws.sent if "listen" in raw]
    assert sent_payloads and sent_payloads[-1]["text"] == "hi"


@pytest.mark.asyncio
async def test_connection_close_invalidates_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Обрыв WebSocket сбрасывает токены и форсирует повторный OTA."""

    cfg = XiaozhiConfigManager(tmp_path / "xiaozhi.json")
    cfg.update(
        websocket_url="ws://example",
        websocket_token="stale-token",
        device_id="AA:BB:CC:DD:EE:FF",
        client_id="cli",
    )

    class ClosingWebSocket(DummyWebSocket):
        def __init__(self) -> None:
            super().__init__()
            self.exc = ConnectionClosed(
                Close(4401, "unauthorized"), Close(4401, "unauthorized"), rcvd_then_sent=True
            )

        def __aiter__(self) -> "ClosingWebSocket":
            return self

        async def __anext__(self) -> str:
            raise self.exc

    ws = ClosingWebSocket()

    async def fake_connect(*_: Any, **__: Any) -> ClosingWebSocket:
        return ws

    monkeypatch.setattr("websockets.connect", fake_connect)

    client = XiaozhiClient(cfg)
    reply = await client.ask_text("hi", trace_id="trace", timeout=1)

    assert reply is None
    assert cfg.get("websocket_token") is None
    assert cfg.get("network")["websocket"]["token"] is None


@pytest.mark.asyncio
async def test_invalid_status_code_resets_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """При ответе 4xx на рукопожатии кэш должен сбрасываться и логироваться."""

    cfg = XiaozhiConfigManager(tmp_path / "xiaozhi.json")
    cfg.update(
        websocket_url="ws://example",
        websocket_token="expired-token",
        device_id="AA:BB:CC:DD:EE:FF",
        client_id="cli",
        efuse={"activation_status": True},
    )

    async def fake_connect(*_: Any, **__: Any) -> None:  # pragma: no cover - исключение вместо возврата
        raise InvalidStatusCode(401, headers={"X-Test": "bad-token"})

    monkeypatch.setattr("websockets.connect", fake_connect)

    client = XiaozhiClient(cfg)
    with pytest.raises(InvalidStatusCode):
        await client._connect(ensure_config=False)

    # После ошибки рукопожатия конфиг должен быть очищен, чтобы следующая
    # попытка запросила свежие параметры OTA.
    assert cfg.get("websocket_url") is None
    assert cfg.get("websocket_token") is None


@pytest.mark.asyncio
async def test_retries_ota_after_websocket_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """При ошибке WebSocket клиент повторно запрашивает OTA и переподключается."""

    cfg = XiaozhiConfigManager(tmp_path / "xiaozhi.json")
    cfg.update(
        websocket_url="ws://stale",  # протухший URL
        websocket_token="stale-token",
        device_id="AA:BB:CC:DD:EE:FF",
        client_id="cli",
        efuse={"activation_status": True},
    )

    # Первый вызов websockets.connect рушится, второй отдаёт нормальный сокет.
    ws = DummyWebSocket()
    await ws.feed(json.dumps({"type": "hello"}))
    await ws.feed(json.dumps({"text": "новый ответ"}))
    await ws.feed(None)

    connect_calls = 0

    async def fake_connect(*_: Any, **__: Any):
        nonlocal connect_calls
        connect_calls += 1
        if connect_calls == 1:
            raise RuntimeError("ws failed")
        return ws

    monkeypatch.setattr("websockets.connect", fake_connect)

    # OTA вызывается только после сброшенного кэша.
    ota_calls = 0

    def fake_post(*_: Any, **__: Any) -> DummyResponse:
        nonlocal ota_calls
        ota_calls += 1
        return DummyResponse({"websocket": {"url": "ws://fresh", "token": "fresh-token"}})

    monkeypatch.setattr("requests.post", fake_post)

    client = XiaozhiClient(cfg)
    reply = await client.ask_text("hi", timeout=1)

    assert reply == "новый ответ"
    assert connect_calls == 2  # Вторая попытка после обновления OTA
    assert ota_calls == 1  # Сходили за новым токеном только после ошибки
    assert cfg.get("websocket_url") == "ws://fresh"
