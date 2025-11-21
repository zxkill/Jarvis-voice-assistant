import json
from types import SimpleNamespace
import threading
from websockets.sync.server import serve

import pytest

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
        # Получаем JSON, убеждаемся что share_code присутствует
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
