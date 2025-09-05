import pytest
from utils.ollama_client import OllamaClient


class DummyResponse:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


def test_generate_light_profile(monkeypatch):
    captured = {}

    def fake_post(url, json, timeout):
        captured["url"] = url
        captured["json"] = json
        return DummyResponse({"response": "hi"})

    monkeypatch.setattr("utils.ollama_client.requests.post", fake_post)
    client = OllamaClient(profiles={"light": "test-model"})
    text = client.generate("hello", profile="light")
    assert text == "hi"
    assert captured["json"]["model"] == "test-model"
    assert "/api/generate" in captured["url"]


def test_generate_unknown_profile():
    client = OllamaClient()
    with pytest.raises(ValueError):
        client.generate("hi", profile="unknown")
