"""Проверки управления локальным воспроизведением в ``working_tts``."""

from __future__ import annotations

import sys
import types

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
