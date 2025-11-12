"""Тесты приёмника аудиопотока робота."""

from __future__ import annotations

import asyncio
import json
import struct

import pytest
import websockets

from audio.robot_stream import RobotAudioStream, RobotStreamClosed, downmix_to_mono

_HEADER = struct.Struct("<2sBBIQIIHHIfffff")
_PLAYBACK_HEADER = struct.Struct("<2sBBIIIHHIIff")


def _build_payload(sequence: int = 1, frame_samples: int = 4) -> bytes:
    """Собрать тестовый бинарный кадр с двумя каналами."""

    pcm = struct.pack("<" + "h" * (frame_samples * 2), *range(frame_samples * 2))
    header = _HEADER.pack(
        b"AF",  # magic
        1,  # version
        0,  # flags
        sequence,
        123456,  # timestamp
        16_000,
        frame_samples,
        2,  # channels
        16,  # sample bits
        len(pcm),
        0.1,
        0.2,
        0.15,
        0.0,
        0.0,
    )
    return header + pcm


def test_downmix_to_mono() -> None:
    """Проверяем, что усреднение двух каналов работает корректно."""

    stereo = struct.pack("<hhhh", 1000, -1000, 2000, -2000)
    mono = downmix_to_mono(stereo, 2)
    # Усреднение пары (1000, -1000) даёт 0, аналогично для второй пары.
    assert mono == struct.pack("<hh", 0, 0)


def test_decode_frame_fields() -> None:
    """Распаковка заголовка возвращает ожидаемые значения."""

    stream = RobotAudioStream("ws://127.0.0.1:8765/")
    payload = _build_payload(sequence=42)
    frame = stream._decode_frame(payload)
    assert frame is not None
    assert frame.sequence == 42
    # Проверяем, что моно-буфер соответствует усреднению каналов.
    expected_mono = downmix_to_mono(payload[_HEADER.size :], 2)
    assert frame.pcm_mono == expected_mono
    assert frame.localization_enabled is False


def test_websocket_server_receives_audio() -> None:
    """Интеграционный тест: сервер принимает кадр и отправляет ack."""

    async def _runner() -> None:
        stream = RobotAudioStream("ws://127.0.0.1:0/robot", queue_max=2)
        await stream.start()
        assert stream._server is not None
        port = stream._server.sockets[0].getsockname()[1]
        payload = _build_payload(sequence=7)

        async with websockets.connect(f"ws://127.0.0.1:{port}/robot") as ws:
            await ws.send(payload)
            ack = await asyncio.wait_for(ws.recv(), timeout=1.0)
            assert json.loads(ack)["sequence"] == 7

        frame = await asyncio.wait_for(stream.read(), timeout=1.0)
        assert frame.sequence == 7
        # После остановки чтение должно вызвать исключение.
        assert stream.stop() is True
        await asyncio.sleep(0.05)
        with pytest.raises(RobotStreamClosed):
            await asyncio.wait_for(stream.read(), timeout=1.0)

    asyncio.run(_runner())


def test_websocket_server_sends_tts_to_robot() -> None:
    """Проверяем, что отправка озвучки формирует бинарный кадр."""

    async def _runner() -> tuple[bytes, int]:
        stream = RobotAudioStream("ws://127.0.0.1:0/robot", queue_max=2)
        await stream.start()
        assert stream._server is not None
        port = stream._server.sockets[0].getsockname()[1]

        async with websockets.connect(f"ws://127.0.0.1:{port}/robot") as ws:
            # PCM двух сэмплов для простоты проверки.
            pcm = struct.pack("<hh", 1200, -1200)
            stream.send_tts(
                pcm,
                16_000,
                text="привет",
                chunk_index=1,
                chunks_total=1,
                volume=1.0,
            )
            payload = await asyncio.wait_for(ws.recv(), timeout=1.0)
        return payload, len(pcm)

    payload, pcm_len = asyncio.run(_runner())

    assert isinstance(payload, (bytes, bytearray))
    header = _PLAYBACK_HEADER.unpack_from(payload)
    (
        magic,
        version,
        flags,
        sequence,
        timestamp_us,
        sample_rate,
        channels,
        sample_bits,
        frame_samples,
        pcm_bytes,
        volume,
        reserved,
    ) = header
    assert magic == b"AP"
    assert version == 1
    assert flags == 0
    assert sample_rate == 16_000
    assert frame_samples == pcm_len // 2
    assert channels == 1
    assert sample_bits == 16
    assert pcm_bytes == pcm_len
    pcm = payload[_PLAYBACK_HEADER.size : _PLAYBACK_HEADER.size + pcm_bytes]
    assert pcm == struct.pack("<hh", 1200, -1200)
    assert volume == pytest.approx(1.0)
    assert reserved == pytest.approx(0.0)


def test_websocket_server_sends_effect_to_robot() -> None:
    """Эффекты эмоций тоже должны доходить до ESP32 через AP-заголовок."""

    async def _runner() -> tuple[bytes, int]:
        stream = RobotAudioStream("ws://127.0.0.1:0/robot", queue_max=2)
        await stream.start()
        assert stream._server is not None
        port = stream._server.sockets[0].getsockname()[1]

        async with websockets.connect(f"ws://127.0.0.1:{port}/robot") as ws:
            pcm = struct.pack("<hhhh", 500, -500, 1000, -1000)
            stream.send_effect(
                pcm,
                22_050,
                name="SIGH",
                source_file="sigh.wav",
                repeat_index=1,
                repeat_total=1,
                volume=0.75,
            )
            payload = await asyncio.wait_for(ws.recv(), timeout=1.0)
        return payload, len(pcm)

    payload, pcm_len = asyncio.run(_runner())

    header = _PLAYBACK_HEADER.unpack_from(payload)
    (
        magic,
        version,
        flags,
        sequence,
        timestamp_us,
        sample_rate,
        channels,
        sample_bits,
        frame_samples,
        pcm_bytes,
        volume,
        reserved,
    ) = header
    assert magic == b"AP"
    assert version == 1
    assert flags == 0
    assert sample_rate == 22_050
    assert channels == 1
    assert sample_bits == 16
    assert frame_samples == pcm_len // 2
    assert pcm_bytes == pcm_len
    assert volume == pytest.approx(0.75)
    assert reserved == pytest.approx(0.0)
