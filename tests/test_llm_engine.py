from core import llm_engine
from context import short_term, long_term
import requests


class DummyQuery:
    """Заглушка HTTP-запроса к Ollama."""

    def __init__(self):
        self.calls = []

    def __call__(self, prompt: str, profile: str, trace_id: str = "") -> str:
        self.calls.append((prompt, profile, trace_id))
        return "ответ"


def clear_short_term():
    # Доступ к внутреннему буферу для очистки между тестами
    short_term._buffer.clear()


def test_think_uses_context_and_light_profile(monkeypatch):
    clear_short_term()
    dummy = DummyQuery()
    monkeypatch.setattr(llm_engine, "_query_ollama", dummy)
    monkeypatch.setattr(long_term, "get_events_by_label", lambda label: ["факт"])
    result = llm_engine.think("тема", trace_id="xyz")
    assert result == "ответ"
    prompt, profile, trace = dummy.calls[-1]
    assert profile == "light"
    assert trace == "xyz"
    assert "тема" in prompt
    assert "факт" in prompt
    assert short_term.get_last()[-1]["reply"] == "ответ"


def test_summarise_saves_to_long_term(monkeypatch):
    clear_short_term()
    dummy = DummyQuery()
    saved = []
    monkeypatch.setattr(llm_engine, "_query_ollama", dummy)
    monkeypatch.setattr(long_term, "add_daily_event", lambda text, labels: saved.append((text, list(labels))))
    result = llm_engine.summarise("текст", labels=["news"])
    assert result == "ответ"
    assert dummy.calls[-1][1] == "heavy"
    assert saved == [("ответ", ["news"])]


def test_mood_uses_history(monkeypatch):
    clear_short_term()
    dummy = DummyQuery()
    history = ["хорошо"]
    saved = []
    monkeypatch.setattr(llm_engine, "_query_ollama", dummy)
    monkeypatch.setattr(long_term, "get_events_by_label", lambda label: history if label == "mood" else [])
    monkeypatch.setattr(long_term, "add_daily_event", lambda text, labels: saved.append(text))
    result = llm_engine.mood("радость")
    assert result == "ответ"
    prompt, _, _ = dummy.calls[-1]
    assert "радость" in prompt
    assert "хорошо" in prompt
    assert saved == ["ответ"]


def test_query_falls_back_to_generate(monkeypatch):
    """При 404 от нового эндпоинта должен использоваться старый /api/generate."""

    clear_short_term()
    calls = []

    class Resp404:
        status_code = 404

        def raise_for_status(self):
            raise requests.HTTPError("404")

        def json(self):
            return {}

    class RespOK:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"response": "привет"}

    def fake_post(url, json, timeout):
        calls.append(url)
        return Resp404() if url.endswith("/v1/chat/completions") else RespOK()

    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr(long_term, "get_events_by_label", lambda label: [])
    monkeypatch.setattr(long_term, "add_daily_event", lambda text, labels: None)

    result = llm_engine.think("тема", trace_id="123")
    assert result == "привет"
    assert calls == [
        f"{llm_engine.BASE_URL}/v1/chat/completions",
        f"{llm_engine.BASE_URL}/api/generate",
    ]
