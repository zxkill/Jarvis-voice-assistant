import struct
import wave

import numpy as np
import pytest

from emotion import sounds


class _DummySoundDevice:
    """Простая заглушка ``sounddevice`` для отслеживания вызовов."""

    def __init__(self) -> None:
        self.play_calls: list[tuple[np.ndarray, int, bool]] = []

    def play(self, data: np.ndarray, rate: int, blocking: bool = False) -> None:
        self.play_calls.append((data.copy(), rate, blocking))


def test_play_effect_streams_to_robot(tmp_path, monkeypatch) -> None:
    """При отключённом локальном выводе эффект уходит только в поток."""

    # Готовим WAV c четырьмя сэмплами.
    wav_path = tmp_path / "effect.wav"
    with wave.open(str(wav_path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16_000)
        wf.writeframes(struct.pack("<hhhh", 0, 8000, -8000, 0))

    dummy_sd = _DummySoundDevice()
    monkeypatch.setattr(sounds, "sd", dummy_sd, raising=False)
    monkeypatch.setattr(sounds, "_EFFECTS", {"TEST": sounds._Effect([str(wav_path)], 0.0, 0.0)}, raising=False)
    monkeypatch.setattr(sounds, "_ALIASES", {"TEST": "TEST"}, raising=False)
    monkeypatch.setattr(sounds, "_GLOBAL_LIMITER", None, raising=False)
    monkeypatch.setattr(sounds, "is_quiet_now", lambda: False, raising=False)

    received: list[tuple[bytes, int, dict]] = []

    def _listener(pcm: bytes, sample_rate: int, **meta) -> None:
        received.append((pcm, sample_rate, meta))

    sounds.set_local_playback_enabled(False)
    sounds.register_stream_listener(_listener)
    try:
        sounds.play_effect("TEST")
    finally:
        sounds.unregister_stream_listener(_listener)
        sounds.set_local_playback_enabled(True)

    # Проверяем, что локальное воспроизведение не запускалось.
    assert dummy_sd.play_calls == []

    # И что слушатель получил корректный PCM.
    assert len(received) == 1
    pcm, sr, meta = received[0]
    assert sr == 16_000
    ints = struct.unpack("<{}h".format(len(pcm) // 2), pcm)
    assert ints == pytest.approx((0, 8000, -8000, 0), abs=1)
    assert meta["name"] == "TEST"
    assert meta["repeat_index"] == 1
    assert meta["repeat_total"] == 1
