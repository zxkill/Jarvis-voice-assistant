from core import llm_engine
from context import short_term, long_term
import requests
import pytest


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
    # Заглушаем поиск по долговременной памяти, чтобы не обращаться к БД
    monkeypatch.setattr(
        llm_engine.long_memory,
        "retrieve_similar",
        lambda query, top_k=5: [],
    )
    result = llm_engine.think("тема", trace_id="xyz")
    assert result == "ответ"
    prompt, profile, trace = dummy.calls[-1]
    assert profile == "light"
    assert trace == "xyz"
    assert "тема" in prompt
    assert "факт" in prompt
    assert short_term.get_last()[-1]["reply"] == "ответ"


def test_think_includes_similar_events(monkeypatch):
    """Проверяем, что функция ``think`` подставляет релевантные воспоминания."""
    clear_short_term()
    dummy = DummyQuery()
    queries: list[str] = []

    def fake_retrieve(query: str, top_k: int = 5):
        queries.append(query)
        return [("старое воспоминание", 0.9)]

    monkeypatch.setattr(llm_engine, "_query_ollama", dummy)
    monkeypatch.setattr(long_term, "get_events_by_label", lambda label: [])
    monkeypatch.setattr(llm_engine.long_memory, "retrieve_similar", fake_retrieve)

    result = llm_engine.think("новая тема", trace_id="abc")
    assert result == "ответ"
    prompt, _, _ = dummy.calls[-1]
    assert "старое воспоминание" in prompt
    assert queries == ["новая тема"]


def test_act_includes_similar_events(monkeypatch):
    """Проверяем, что ``act`` расширяет контекст через поиск по памяти."""
    clear_short_term()
    dummy = DummyQuery()
    queries: list[str] = []

    def fake_retrieve(query: str, top_k: int = 5):
        queries.append(query)
        return [("прошлая команда", 0.8)]

    monkeypatch.setattr(llm_engine, "_query_ollama", dummy)
    monkeypatch.setattr(long_term, "get_events_by_label", lambda label: [])
    monkeypatch.setattr(llm_engine.long_memory, "retrieve_similar", fake_retrieve)

    result = llm_engine.act("сделай что-то", trace_id="def")
    assert result == "ответ"
    prompt, _, _ = dummy.calls[-1]
    assert "прошлая команда" in prompt
    assert queries == ["сделай что-то"]


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

    def fake_post(url, json, headers=None, timeout=60):
        # фиксируем URL вызова и игнорируем переданные заголовки
        calls.append(url)
        return Resp404() if url.endswith("/v1/chat/completions") else RespOK()

    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr(long_term, "get_events_by_label", lambda label: [])
    monkeypatch.setattr(long_term, "add_daily_event", lambda text, labels: None)
    monkeypatch.setattr(
        llm_engine.long_memory, "retrieve_similar", lambda query, top_k=5: []
    )

    result = llm_engine.think("тема", trace_id="123")
    assert result == "привет"
    assert calls == [
        f"{llm_engine.BASE_URL}/v1/chat/completions",
        f"{llm_engine.BASE_URL}/api/generate",
    ]


def test_query_reports_model_not_found(monkeypatch):
    """При 404 'model not found' выдаётся понятная ошибка без повторных запросов."""

    clear_short_term()
    calls = []

    class Resp404:
        status_code = 404

        def raise_for_status(self):
            raise requests.HTTPError("404")

        def json(self):
            return {"error": "model not found: llama2"}

        @property
        def text(self):
            return "model not found: llama2"

    def fake_post(url, json, headers=None, timeout=60):
        calls.append(url)
        return Resp404()

    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr(
        llm_engine.long_memory, "retrieve_similar", lambda query, top_k=5: []
    )

    with pytest.raises(RuntimeError) as exc:
        llm_engine.think("тема", trace_id="id42")

    assert "модель" in str(exc.value).lower()
    # Убедимся, что повторного вызова на /api/generate не было
    assert calls == [f"{llm_engine.BASE_URL}/v1/chat/completions"]
