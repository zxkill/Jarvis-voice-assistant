import asyncio
import importlib
import sys
from types import SimpleNamespace

from core import llm_engine


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

    dummy_tts = SimpleNamespace(speak_async=fake_speak, MAX_CHARS=10)
    monkeypatch.setitem(sys.modules, "working_tts", dummy_tts)
    monkeypatch.delitem(sys.modules, "app.command_processing", raising=False)
    cp = importlib.import_module("app.command_processing")

    monkeypatch.setattr(cp.llm_engine, "stream_think", fake_stream)
    monkeypatch.setattr(cp, "_reset_chat_timer", lambda: None)
    monkeypatch.setattr(cp, "get_request_source", lambda: "voice")

    asyncio.run(cp._chat_llm("привет"))

    assert calls == ["Привет.", "Как дела?"]
    assert cp.CHAT_HISTORY[-1][1] == "Привет. Как дела?"


def test_chat_llm_streams_to_telegram(monkeypatch):
    sent = []
    actions = []

    def fake_stream(topic, trace_id):
        yield "Привет. "
        yield "Как дела?"

    dummy_tts = SimpleNamespace(speak_async=lambda *a, **k: None, MAX_CHARS=10)
    monkeypatch.setitem(sys.modules, "working_tts", dummy_tts)
    monkeypatch.delitem(sys.modules, "app.command_processing", raising=False)
    cp = importlib.import_module("app.command_processing")

    monkeypatch.setattr(cp.llm_engine, "stream_think", fake_stream)
    monkeypatch.setattr(cp, "_reset_chat_timer", lambda: None)
    monkeypatch.setattr(cp, "get_request_source", lambda: "telegram")

    cfg = SimpleNamespace(telegram=SimpleNamespace(token="T"), user=SimpleNamespace(telegram_user_id=1))
    monkeypatch.setattr("core.config.load_config", lambda: cfg)
    monkeypatch.delitem(sys.modules, "notifiers.telegram", raising=False)
    import notifiers.telegram as telegram

    monkeypatch.setattr(telegram, "send", lambda text: sent.append(text))
    monkeypatch.setattr(telegram, "send_action", lambda action="typing": actions.append(action))

    asyncio.run(cp._chat_llm("привет"))

    assert sent == ["Привет.", "Как дела?"]
    assert actions == ["typing", "typing"]
