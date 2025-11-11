"""Тесты приёмника аудиопотока робота."""

from __future__ import annotations

import asyncio
import json
import struct

import pytest
import websockets

from audio.robot_stream import RobotAudioStream, RobotStreamClosed, downmix_to_mono

_HEADER = struct.Struct("<2sBBIQIIHHIfffff")


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


@pytest.mark.asyncio
async def test_websocket_server_receives_audio() -> None:
    """Интеграционный тест: сервер принимает кадр и отправляет ack."""

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
