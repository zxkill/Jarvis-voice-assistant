import struct
import wave

import pytest

from emotion import sounds


def test_read_wav_downmixes_stereo(tmp_path):
    """Стерео WAV приводится к моно, чтобы робот не получал интерливированный звук."""

    path = tmp_path / "stereo.wav"
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(16_000)
        # Два стерео-фрейма: (1000, -1000) и (2000, -2000) → среднее = 0.
        wf.writeframes(struct.pack("<hhhh", 1000, -1000, 2000, -2000))

    data, rate, channels = sounds._read_wav(str(path))

    assert rate == 16_000
    assert channels == 2
    assert data.tolist() == pytest.approx([0.0, 0.0])

    pcm = sounds._float32_to_pcm16(data)
    assert struct.unpack("<2h", pcm) == (0, 0)


def test_read_wav_converts_unsigned_8bit(tmp_path):
    """8-битный unsigned WAV конвертируется в PCM16 со смещением середины."""

    path = tmp_path / "unsigned.wav"
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(1)
        wf.setframerate(8_000)
        # Значения 0/128/255 → должны превратиться в -1.0/0/+1.0 (с оговоркой о клиппинге).
        wf.writeframes(bytes([0, 128, 255]))

    data, rate, channels = sounds._read_wav(str(path))

    assert rate == 8_000
    assert channels == 1
    assert data.tolist() == pytest.approx([-1.0, 0.0, 127 / 128], rel=1e-4)

    pcm = sounds._float32_to_pcm16(data)
    values = struct.unpack("<3h", pcm)
    assert values[0] < 0 and values[2] > 0
    assert values[1] == 0
