from __future__ import annotations
import asyncio
from pathlib import Path
import sys

sys.path.append(str(Path(__file__).resolve().parents[2]))

def test_log_and_fetch_history(dialog_db):
    from memory import dialogs

    trace_id = "trace-1"
    inserted = dialogs.log_message(
        "Привет",
        direction="incoming",
        channel="telegram",
        trace_id=trace_id,
        metadata={"source": "unit"},
    )
    assert inserted > 0

    history = dialogs.fetch_history(trace_id=trace_id, limit=10)
    assert len(history) == 1
    item = history[0]
    assert item["text"] == "Привет"
    assert item["direction"] == "incoming"
    assert item["channel"] == "telegram"
    assert item["meta"]["source"] == "unit"


def test_va_respond_logs_dialog(monkeypatch, dialog_db):
    from memory import dialogs
    from types import SimpleNamespace

    spoken: list[str] = []

    async def fake_speak(text: str, **kwargs):  # noqa: D401 - заглушка для синтеза
        spoken.append(text)

    dummy_skills = SimpleNamespace(handle_utterance=lambda text: False, set_main_loop=lambda loop: None)
    dummy_nlp = SimpleNamespace(normalize=lambda value: value)
    dummy_tts = SimpleNamespace(speak_async=fake_speak)

    monkeypatch.setitem(sys.modules, "jarvis_skills", dummy_skills)
    monkeypatch.setitem(sys.modules, "core.nlp", dummy_nlp)
    monkeypatch.setitem(sys.modules, "working_tts", dummy_tts)
    monkeypatch.delitem(sys.modules, "app.command_processing", raising=False)

    from app import command_processing as cp

    async def fake_recognize(raw: str):
        return {"cmd": "thanks", "percent": 100}

    monkeypatch.setattr(cp, "recognize_cmd", fake_recognize)

    asyncio.run(cp.va_respond("Джарвис спасибо"))

    history = dialogs.fetch_history(limit=5, ascending=True)
    assert [item["direction"] for item in history] == ["incoming", "outgoing"]
    assert history[0]["text"].lower() == "джарвис спасибо"
    assert history[1]["text"] == "Пожалуйста"
    assert spoken == ["Пожалуйста"]
