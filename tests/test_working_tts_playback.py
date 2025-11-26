"""Проверки управления локальным воспроизведением в ``working_tts``."""

from __future__ import annotations

import sys
import types
import wave
from pathlib import Path

import pytest
import numpy as np


class _StubSoundDevice(types.ModuleType):
    """Минимальная заглушка ``sounddevice`` для изоляции тестов."""

    def __init__(self) -> None:
        super().__init__("sounddevice")

    def play(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
        return None

    def stop(self) -> None:
        return None


sys.modules.setdefault("sounddevice", _StubSoundDevice())


class _StubPiper(types.ModuleType):
    """Упрощённая заглушка Piper, возвращающая фиктивный голос."""

    class PiperVoice:  # type: ignore[too-few-public-methods]
        def __init__(self) -> None:
            self.config = types.SimpleNamespace(sample_rate=16000)

        @classmethod
        def load(cls, *_args, **_kwargs):  # type: ignore[no-untyped-def]
            return cls()

        def synthesize(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
            return np.zeros(1, dtype=np.int16)


sys.modules.setdefault("piper", _StubPiper("piper"))


class _StubTransliterate(types.ModuleType):
    def translit(self, text: str, _lang: str, reversed: bool = False) -> str:  # type: ignore[override]
        return text


sys.modules.setdefault("transliterate", _StubTransliterate("transliterate"))


class _StubKeyboard(types.ModuleType):
    def is_pressed(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
        return False


sys.modules.setdefault("keyboard", _StubKeyboard("keyboard"))


class _StubCoreNLP(types.ModuleType):
    def normalize_tts_text(self, text: str) -> str:
        return text


sys.modules.setdefault("core.nlp", _StubCoreNLP("core.nlp"))

import working_tts


class _DummySoundDevice:
    """Заглушка ``sounddevice`` для проверки вызовов воспроизведения."""

    def __init__(self) -> None:
        self.play_calls: list[tuple] = []
        self.stop_calls: int = 0

    def play(self, audio, rate, blocking=False):  # type: ignore[no-untyped-def]
        self.play_calls.append((audio, rate, blocking))

    def stop(self) -> None:
        self.stop_calls += 1


def test_local_playback_enabled_triggers_sounddevice(monkeypatch) -> None:
    """Проверяем, что при включённом флаге задействуется ``sounddevice``."""

    dummy = _DummySoundDevice()
    monkeypatch.setattr(working_tts, "sd", dummy, raising=False)
    working_tts._STOP_EVENT.clear()
    working_tts.set_local_playback_enabled(True)

    audio = np.zeros(8, dtype=np.float32)
    working_tts._perform_playback(audio, 16000, 0.0)

    assert dummy.play_calls, "ожидали вызов sounddevice.play при включённом флаге"
    assert dummy.stop_calls == 1, "ожидали остановку воспроизведения"
    working_tts.set_local_playback_enabled(True)


def test_local_playback_disabled_skips_sounddevice(monkeypatch) -> None:
    """При отключении локального вывода ``sounddevice`` не должен вызываться."""

    class _FailingSoundDevice:
        def play(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
            raise AssertionError("play() не должен вызываться при отключении")

        def stop(self) -> None:
            raise AssertionError("stop() не должен вызываться при отключении")

    monkeypatch.setattr(working_tts, "sd", _FailingSoundDevice(), raising=False)
    working_tts._STOP_EVENT.clear()
    working_tts.set_local_playback_enabled(False)

    audio = np.zeros(8, dtype=np.float32)
    working_tts._perform_playback(audio, 16000, 0.0)

    # Отсутствие исключений означает, что ``sounddevice`` не вызывался.
    working_tts.set_local_playback_enabled(True)


def test_resample_for_stream_downsamples_to_16k() -> None:
    """Ресемплинг TTS до 16 кГц уменьшает объём кадров и выставляет целевую частоту."""

    # Формируем простой линейный сигнал, чтобы audioop корректно его пересчитал.
    pcm_src = np.arange(0, 20, dtype=np.int16).tobytes()
    out_pcm, out_rate = working_tts._resample_for_stream(pcm_src, 44100, working_tts.STREAM_SAMPLE_RATE)

    in_frames = len(pcm_src) // 2
    out_frames = len(out_pcm) // 2
    expected_frames = int(round(in_frames * working_tts.STREAM_SAMPLE_RATE / 44100))

    assert out_rate == working_tts.STREAM_SAMPLE_RATE, "Частота должна быть приведена к 16 кГц"
    # Разрешаем погрешность в 1 кадр из-за округления внутри audioop.
    assert out_frames in {expected_frames, max(1, expected_frames - 1), expected_frames + 1}


def test_resample_for_stream_passthrough_same_rate() -> None:
    """Если частоты совпадают, PCM передаётся как есть без лишних искажений."""

    pcm_src = b"\x01\x00\x02\x00\x03\x00\x04\x00"
    out_pcm, out_rate = working_tts._resample_for_stream(pcm_src, working_tts.STREAM_SAMPLE_RATE, working_tts.STREAM_SAMPLE_RATE)

    assert out_rate == working_tts.STREAM_SAMPLE_RATE
    assert out_pcm == pcm_src


def test_save_wav_resamples_to_stream_rate(tmp_path: Path) -> None:
    """WAV, сохранённый для отладки, конвертируется в 16 кГц PCM16 LE."""

    src_rate = 22050
    pcm_src = np.arange(0, 1000, dtype=np.int16)
    wav_path = tmp_path / "tts.wav"

    working_tts._save_wav(str(wav_path), pcm_src, sample_rate=src_rate)

    with wave.open(str(wav_path), "rb") as wf:
        assert wf.getframerate() == working_tts.STREAM_SAMPLE_RATE
        assert wf.getnchannels() == 1
        frames = wf.readframes(wf.getnframes())

    expected_frames = int(round(pcm_src.size * working_tts.STREAM_SAMPLE_RATE / src_rate))
    assert wf.getnframes() in {expected_frames, max(1, expected_frames - 1), expected_frames + 1}
    assert len(frames) > 0, "Ресемплированный WAV не должен быть пустым"
