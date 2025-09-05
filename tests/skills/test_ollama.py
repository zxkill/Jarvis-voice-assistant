from collections import deque

import requests
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from skills import ollama
from core import llm_engine
from context import short_term, long_term


class DummyResponse:
    """Простая заглушка объекта ``requests.Response``.

    Содержит только необходимый минимум: статус-код, метод ``json`` и
    ``raise_for_status`` для имитации поведения библиотеки ``requests``.
    """

    def __init__(self, data, status_code=200):
        self._data = data
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"status {self.status_code}")

    def json(self):
        return self._data


def reset_short_term():
    """Очистить краткосрочную память между тестами."""
    short_term._buffer = deque(maxlen=short_term.BUFFER_SIZE)


def test_handle_routes_to_think(monkeypatch):
    calls = []

    def fake_think(text, trace_id):
        calls.append((text, trace_id))
        return "ответ"

    monkeypatch.setattr(llm_engine, "think", fake_think)
    result = ollama.handle("Расскажи сказку", trace_id="1")
    assert result == "ответ"
    assert calls == [("Расскажи сказку", "1")]


def test_handle_routes_to_act(monkeypatch):
    calls = []

    def fake_act(text, trace_id):
        calls.append((text, trace_id))
        return "готово"

    monkeypatch.setattr(llm_engine, "act", fake_act)
    result = ollama.handle("Сделай шаг", trace_id="xyz")
    assert result == "готово"
    assert calls == [("Сделай шаг", "xyz")]


def test_handle_returns_error_message(monkeypatch):
    """Скилл должен возвращать понятное сообщение при сбое LLM."""

    def fake_think(text, trace_id):
        raise RuntimeError("Ollama недоступна")

    monkeypatch.setattr(llm_engine, "think", fake_think)
    reply = ollama.handle("Расскажи анекдот", trace_id="99")
    assert "недоступ" in reply.lower()


def test_handle_reports_missing_model(monkeypatch):
    """Скилл сообщает о нехватке модели, если LLM подняла ошибку."""

    def fake_think(text, trace_id):
        raise RuntimeError("Модель llama2 не найдена")

    monkeypatch.setattr(llm_engine, "think", fake_think)
    reply = ollama.handle("Расскажи сказку", trace_id="77")
    assert "не найдена" in reply.lower()


def test_think_saves_dialog(monkeypatch):
    reset_short_term()
    saved = []

    def fake_post(url, json, headers=None, timeout=60):
        # сохраняем переданные данные для последующей проверки
        fake_post.last_payload = json
        fake_post.last_headers = headers or {}
        return DummyResponse({"choices": [{"message": {"content": "привет"}}]})

    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr(long_term, "add_daily_event", lambda text, labels: saved.append((text, labels)))

    reply = llm_engine.think("Как дела?", trace_id="42")
    assert reply == "привет"
    assert fake_post.last_headers["X-Trace-Id"] == "42"
    assert fake_post.last_payload["stream"] is False
    assert "Как дела?" in fake_post.last_payload["messages"][0]["content"]
    assert short_term.get_last()[-1] == {
        "trace_id": "42",
        "user": "Как дела?",
        "reply": "привет",
    }
    assert saved == [("user: Как дела?\nassistant: привет", ["think"])]
