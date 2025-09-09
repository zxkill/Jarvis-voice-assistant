import asyncio

from core import llm_engine
from app import command_processing as cp


def test_stream_think_yields_and_logs(monkeypatch):
    chunks = ["привет, ", "мир!"]

    def fake_query(prompt, profile, trace_id=""):
        for c in chunks:
            yield c

    monkeypatch.setattr(llm_engine, "_query_ollama_stream", fake_query)
    monkeypatch.setattr(llm_engine, "_compose_context", lambda: "ctx")
    monkeypatch.setattr(llm_engine.long_term, "get_events_by_label", lambda label: [])
    monkeypatch.setattr(llm_engine.long_memory, "retrieve_similar", lambda q: [])
    monkeypatch.setattr(llm_engine.preferences, "load_preferences", lambda: [])

    records = {}

    def fake_short(data):
        records["short"] = data

    def fake_long(text, labels):
        records["long"] = (text, labels)

    def fake_log(stage, prompt, reply, trace_id, context):
        records["log"] = reply

    monkeypatch.setattr(llm_engine.short_term, "add", fake_short)
    monkeypatch.setattr(llm_engine.long_term, "add_daily_event", fake_long)
    monkeypatch.setattr(llm_engine, "_log_llm_exchange", fake_log)

    result = list(llm_engine.stream_think("тест", trace_id="id"))
    assert result == chunks
    assert records["short"]["reply"] == "привет, мир!"
    assert records["long"][0].endswith("привет, мир!")
    assert records["log"] == "привет, мир!"


def test_chat_llm_streams_to_tts(monkeypatch):
    calls = []

    async def fake_speak(text, **kwargs):
        calls.append(text)

    def fake_stream(topic, trace_id):
        yield "Привет. "
        yield "Как дела?"

    monkeypatch.setattr(cp.llm_engine, "stream_think", fake_stream)
    monkeypatch.setattr(cp, "speak_async", fake_speak)
    monkeypatch.setattr(cp, "_reset_chat_timer", lambda: None)

    asyncio.run(cp._chat_llm("привет"))

    assert calls == ["Привет.", "Как дела?"]
    assert cp.CHAT_HISTORY[-1][1] == "Привет. Как дела?"
