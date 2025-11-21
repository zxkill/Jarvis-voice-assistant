import json
import threading
from types import SimpleNamespace
from typing import List

import pytest
from websockets.sync.server import serve

from core.xiaozhi_client import XiaozhiClient, XiaozhiSettings


class DummyResponse:
    def __init__(self, status_code: int = 200, body: str | None = None):
        self.status_code = status_code
        self.body = body or json.dumps({"text": "ok"})
        self.ok = status_code == 200
        self.text = self.body

    def json(self):  # pragma: no cover - страхуемся от ValueError
        return json.loads(self.body)


def test_extracts_text_and_headers(monkeypatch):
    captured = SimpleNamespace(payload=None, headers=None)

    def fake_post(url, json=None, headers=None, timeout=None):  # noqa: A002
        captured.payload = json
        captured.headers = headers
        return DummyResponse()

    monkeypatch.setattr("requests.post", fake_post)

    settings = XiaozhiSettings(endpoint="https://example.com", agent_code="abc", timeout=5)
    client = XiaozhiClient(settings)
    reply = client.ask("ping", trace_id="trace-1")

    assert reply == "ok"
    assert captured.payload == {"share_code": "abc", "input": "ping", "stream": False}
    assert captured.headers["X-Trace-Id"] == "trace-1"
    assert captured.headers["device-id"] == "jarvis-client"
    assert captured.headers["client-id"] == "jarvis-client"


def test_missing_text_raises(monkeypatch):
    def fake_post(url, json=None, headers=None, timeout=None):  # noqa: A002
        return DummyResponse(body=jsonlib.dumps({"data": {}}))

    # используем json напрямую, чтобы симулировать невалидный ответ
    jsonlib = __import__("json")
    monkeypatch.setattr("requests.post", fake_post)

    settings = XiaozhiSettings(endpoint="https://example.com", agent_code="abc", timeout=5)
    client = XiaozhiClient(settings)

    with pytest.raises(RuntimeError):
        client.ask("ping")


def test_websocket_roundtrip():
    """Проверяем отправку и получение ответа по WebSocket."""

    def handler(websocket):
        # Сервер сперва принимает hello, отвечает hello и только потом ждёт payload
        hello = json.loads(websocket.recv())
        assert hello["type"] == "hello"
        websocket.send(json.dumps({"type": "hello", "transport": "websocket"}))
        payload = json.loads(websocket.recv())
        assert payload["share_code"] == "ws-code"
        websocket.send(json.dumps({"text": f"echo-{payload['input']}", "done": True}))

    with serve(handler, "127.0.0.1", 0) as server:
        port = server.socket.getsockname()[1]
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()

        settings = XiaozhiSettings(endpoint=f"ws://127.0.0.1:{port}", agent_code="ws-code", timeout=3)
        client = XiaozhiClient(settings)

        try:
            assert client.ask("hello") == "echo-hello"
        finally:
            server.shutdown()
            worker.join(timeout=3)


def test_http_404_triggers_ws_autoconversion(monkeypatch):
    """HTTP 404 должен конвертировать URL в WebSocket и вернуть ответ."""

    events: List[str] = []

    def fake_post(url, json=None, headers=None, timeout=None):  # noqa: A002
        events.append("http")
        return DummyResponse(status_code=404, body="{}")

    def fake_connect(url, additional_headers=None, open_timeout=None, close_timeout=None):  # noqa: ANN001
        events.append(url)

        class DummyWs:
            def __enter__(self):  # noqa: D401
                # Заголовки должны содержать авторизацию и версию
                assert additional_headers["Authorization"] == "Bearer abc"
                assert additional_headers["Protocol-Version"] == "1"
                assert additional_headers["device-id"] == "jarvis-client"
                assert additional_headers["client-id"] == "jarvis-client"
                return self

            def __exit__(self, exc_type, exc, tb):  # noqa: D401
                return False

            def send(self, payload):  # noqa: D401
                # Первое сообщение — hello, второе — основная нагрузка
                data = jsonlib.loads(payload)
                if data.get("type") == "hello":
                    events.append("hello")
                else:
                    assert data["share_code"] == "abc"

            def recv(self, timeout=None):  # noqa: D401
                if "hello" in events and "hello-sent" not in events:
                    events.append("hello-sent")
                    return json.dumps({"type": "hello", "transport": "websocket"})

                events.append("reply")
                return json.dumps({"text": "from-ws", "done": True})

        return DummyWs()

    jsonlib = __import__("json")
    monkeypatch.setattr("requests.post", fake_post)
    monkeypatch.setattr("core.xiaozhi_client.connect", fake_connect)

    settings = XiaozhiSettings(endpoint="https://example.com/xiaozhi/chat", agent_code="abc", timeout=2)
    client = XiaozhiClient(settings)

    reply = client.ask("ping")
    assert reply == "from-ws"
    assert events[1] == "wss://example.com/xiaozhi/chat/ws"


def test_derives_ws_endpoint_from_http():
    """Проверяем чистую конвертацию http → ws."""

    settings = XiaozhiSettings(endpoint="http://demo.local/xz/api", agent_code="abc", timeout=1)
    client = XiaozhiClient(settings)

    assert client._derive_ws_endpoint() == "ws://demo.local/xz/api/ws"


def test_derives_official_ws_endpoint_for_tenclass():
    """Официальный CDN api.tenclass.net должен переводиться в /xiaozhi/v1/."""

    settings = XiaozhiSettings(endpoint="https://api.tenclass.net/xiaozhi/chat", agent_code="abc", timeout=1)
    client = XiaozhiClient(settings)

    assert client._derive_ws_endpoint() == "wss://api.tenclass.net/xiaozhi/v1/"
