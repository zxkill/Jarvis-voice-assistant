import io
import json
import sys
import wave
from types import SimpleNamespace

import pytest

from core.xiaozhi_audio import chat_via_audio, synthesize_text_to_wav, transcribe_wav_vosk


def test_synthesize_text_to_wav_collects_pcm(monkeypatch):
    """Проверяем, что helper собирает PCM и упаковывает в WAV."""

    listeners = []
    playback_state = {"enabled": True}

    def register(listener):
        listeners.append(listener)

    def unregister(listener):
        listeners.remove(listener)

    def set_local(flag):
        playback_state["enabled"] = flag

    def fake_tts(text, preset="neutral"):
        for listener in list(listeners):
            listener(b"\x01\x02" * 4, 16000, text=text, preset=preset, chunk_index=0, chunks_total=1, volume=1.0)

    dummy = SimpleNamespace(
        _LOCAL_PLAYBACK_ENABLED=True,
        register_stream_listener=register,
        unregister_stream_listener=unregister,
        set_local_playback_enabled=set_local,
        working_tts=fake_tts,
    )
    monkeypatch.setitem(sys.modules, "working_tts", dummy)

    wav_bytes, rate = synthesize_text_to_wav("привет", trace_id="t1")
    assert rate == 16000
    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        assert wf.getnframes() > 0
        assert wf.getframerate() == 16000


def test_transcribe_wav_vosk(monkeypatch):
    """Транскрипция использует Vosk и возвращает текст."""

    class DummyRecognizer:
        def __init__(self, model, rate):
            self.calls = []

        def AcceptWaveform(self, pcm):  # noqa: N802
            self.calls.append(len(pcm))

        def Result(self):
            return json.dumps({"text": "ответ"})

    dummy_vosk = SimpleNamespace(Model=lambda path: object(), KaldiRecognizer=lambda model, rate: DummyRecognizer(model, rate))
    monkeypatch.setitem(sys.modules, "vosk", dummy_vosk)

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(b"\x01\x02" * 10)

    text = transcribe_wav_vosk(buffer.getvalue(), trace_id="t2")
    assert text == "ответ"


def test_chat_via_audio_pipeline(monkeypatch):
    """Полный цикл: синтез → отправка в клиента → транскрипция."""

    monkeypatch.setattr("core.xiaozhi_audio.synthesize_text_to_wav", lambda text, trace_id="": (b"req", 16000))
    monkeypatch.setattr("core.xiaozhi_audio.transcribe_wav_vosk", lambda data, trace_id="": "отвечаю")

    class DummyClient:
        def __init__(self):
            self.seen = []

        def ask_audio(self, wav_bytes, trace_id=""):
            self.seen.append((wav_bytes, trace_id))
            return b"resp"

    client = DummyClient()
    text = chat_via_audio(client, "вопрос", trace_id="t3")
    assert text == "отвечаю"
    assert client.seen == [(b"req", "t3")]

