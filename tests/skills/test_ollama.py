import json
from collections import deque

import requests

from collections import deque
import pathlib
import sys

import requests

ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from skills import ollama
from core import llm_engine
from context import short_term, long_term


class DummyResponse:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

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


def test_think_saves_dialog(monkeypatch):
    reset_short_term()
    saved = []

    def fake_post(url, json, timeout):
        fake_post.last_payload = json
        return DummyResponse({"response": "привет"})

    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr(long_term, "add_daily_event", lambda text, labels: saved.append((text, labels)))

    reply = llm_engine.think("Как дела?", trace_id="42")
    assert reply == "привет"
    assert fake_post.last_payload["trace_id"] == "42"
    assert short_term.get_last()[-1] == {
        "trace_id": "42",
        "user": "Как дела?",
        "reply": "привет",
    }
    assert saved == [("user: Как дела?\nassistant: привет", ["think"])]
