import json
import threading
from types import SimpleNamespace
from typing import List

import json
import threading
from types import SimpleNamespace
from typing import List

import pytest
from websockets.sync.server import serve

from core.xiaozhi_client import (
    XiaozhiBindingRequired,
    XiaozhiClient,
    XiaozhiSettings,
)


@pytest.fixture()
def dummy_opus(monkeypatch):
    """Подменяет загрузку opuslib, чтобы тесты не требовали системный libopus."""

    class DummyEncoder:
        def __init__(self, *_args, **_kwargs):
            pass

        def encode(self, frame, *_args, **_kwargs):  # noqa: D401
            return frame

    class DummyDecoder:
        def __init__(self, *_args, **_kwargs):
            pass

        def decode(self, frame, *_args, **_kwargs):  # noqa: D401
            return frame

    monkeypatch.setattr(
        XiaozhiClient,
        "_ensure_opus_loaded",
        lambda self: (DummyEncoder, DummyDecoder),
        raising=False,
    )


def _build_dummy_wav() -> bytes:
    import io
    import wave

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(b"\x01\x00" * 960)  # 60 мс моно PCM
    return buffer.getvalue()


class DummyResponse:
    def __init__(self, status_code: int = 200, body: str | bytes | None = None):
        self.status_code = status_code
        self.body = body or json.dumps({"text": "ok"})
        self.ok = status_code == 200
        self.text = self.body if isinstance(self.body, str) else ""
        self.content = self.body if isinstance(self.body, (bytes, bytearray)) else str(self.body).encode()

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
    assert captured.headers["Authorization"] == "Bearer abc"


def test_bind_code_is_raised(monkeypatch):
    """Если manager-api возвращает код 10042, клиент должен выбросить исключение."""

    class BindResponse:
        def __init__(self):
            self.status_code = 200
            self.ok = True
            self.text = json.dumps({"code": 10042, "msg": "bind 654321 now"})

        def json(self):
            return json.loads(self.text)

    def fake_post(url, json=None, headers=None, timeout=None):  # noqa: A002
        return BindResponse()

    monkeypatch.setattr("requests.post", fake_post)

    settings = XiaozhiSettings(
        endpoint="https://example.com",
        agent_code="abc",
        manager_url="https://manager.example.com",
        manager_secret="s",
        timeout=5,
    )
    client = XiaozhiClient(settings)

    with pytest.raises(XiaozhiBindingRequired) as err:
        client.ask("ping")

    assert err.value.bind_code == "654321"


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
        # Сервер сперва принимает hello, затем полезную нагрузку
        hello = json.loads(websocket.recv())
        assert hello["type"] == "hello"
        websocket.send(json.dumps({"type": "hello", "transport": "websocket", "session_id": "sess"}))
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
                # Заголовки должны содержать авторизацию, device/client и версию
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
                    events.append("hello-sent")
                else:
                    assert data["share_code"] == "abc"

            def recv(self, timeout=None):  # noqa: D401
                if "hello-sent" in events and "hello-recv" not in events:
                    events.append("hello-recv")
                    return json.dumps({"type": "hello", "transport": "websocket", "session_id": "sess"})
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


def test_audio_http(monkeypatch):
    captured = {}

    def fake_post(url, data=None, headers=None, timeout=None):  # noqa: A002
        captured["data"] = data
        captured["headers"] = headers
        return DummyResponse(body=b"binary")

    monkeypatch.setattr("requests.post", fake_post)

    settings = XiaozhiSettings(endpoint="https://example.com/audio", agent_code="abc", timeout=2)
    client = XiaozhiClient(settings)

    reply = client.ask_audio(b"pcm", trace_id="trace-audio")
    assert reply == b"binary"
    assert captured["headers"]["Content-Type"] == "audio/wav"
    assert captured["headers"]["Authorization"] == "Bearer abc"
    assert captured["headers"]["X-Trace-Id"] == "trace-audio"


def test_audio_websocket(monkeypatch, dummy_opus):
    events: List[str] = []

    def fake_opus_to_wav(frames, trace_id=""):
        return b"".join(frames)

    def fake_connect(url, additional_headers=None, open_timeout=None, close_timeout=None):  # noqa: ANN001
        events.append(url)

        class DummyWs:
            def __enter__(self):
                assert additional_headers["device-id"] == "jarvis-client"
                assert additional_headers["client-id"] == "jarvis-client"
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def send(self, payload):  # noqa: D401
                events.append("sent" if isinstance(payload, bytes) else payload)

            def recv(self, timeout=None):  # noqa: D401
                if "hello" not in events:
                    events.append("hello")
                    return jsonlib.dumps({"type": "hello", "session_id": "sess"})
                if "audio" not in events:
                    events.append("audio")
                    return b"part1"
                return None

        return DummyWs()

    jsonlib = __import__("json")
    monkeypatch.setattr("core.xiaozhi_client.connect", fake_connect)
    monkeypatch.setattr(XiaozhiClient, "_opus_frames_to_wav", staticmethod(fake_opus_to_wav))

    settings = XiaozhiSettings(endpoint="ws://localhost:1234", agent_code="abc", timeout=2)
    client = XiaozhiClient(settings)

    reply = client.ask_audio(_build_dummy_wav())
    assert reply == b"part1"
    assert events[0] == "ws://localhost:1234"


def test_audio_requires_agent_code(monkeypatch):
    """Если не задан секрет агента, WebSocket аудио сразу падает с подсказкой."""

    settings = XiaozhiSettings(endpoint="wss://api.tenclass.net/xiaozhi/v1/", agent_code="")
    client = XiaozhiClient(settings)

    with pytest.raises(RuntimeError, match="agent_code"):
        client.ask_audio(b"pcm")


def test_opus_roundtrip_helpers(dummy_opus):
    """WAV → Opus → WAV должен проходить без потери API контракта."""

    settings = XiaozhiSettings(endpoint="wss://example", agent_code="token")
    client = XiaozhiClient(settings)

    wav = _build_dummy_wav()
    frames = client._encode_wav_to_opus(wav)
    assert frames, "Opus кадры должны быть сформированы"

    restored = client._opus_frames_to_wav(frames)
    assert len(restored) > 44  # WAV header + полезная нагрузка
