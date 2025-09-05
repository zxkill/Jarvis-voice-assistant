import pytest
import requests
from utils.ollama_client import OllamaClient


class DummyResponse:
    """Минимальная заглушка ``requests.Response`` для юнит‑тестов."""

    def __init__(self, data, status_code=200):
        self._data = data
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"status {self.status_code}")

    def json(self):
        return self._data


def test_generate_light_profile(monkeypatch):
    captured = {}

    def fake_post(url, json, timeout):
        captured["url"] = url
        captured["json"] = json
        return DummyResponse({"choices": [{"message": {"content": "hi"}}]})

    monkeypatch.setattr("utils.ollama_client.requests.post", fake_post)
    client = OllamaClient(profiles={"light": "test-model"})
    text = client.generate("hello", profile="light")
    assert text == "hi"
    assert captured["json"]["model"] == "test-model"
    assert captured["json"]["messages"][0]["content"] == "hello"
    assert "/v1/chat/completions" in captured["url"]


def test_generate_unknown_profile():
    client = OllamaClient()
    with pytest.raises(ValueError):
        client.generate("hi", profile="unknown")
