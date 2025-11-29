"""Тесты приёмника аудиопотока робота."""

from __future__ import annotations

import asyncio
import asyncio
import json
import math
import struct

import pytest
import websockets

from audio.robot_stream import RobotAudioStream, RobotStreamClosed, downmix_to_mono


def _build_pcm(frame_samples: int = 4, channels: int = 2) -> bytes:
    """Собрать тестовый бинарный PCM-контент с указанным числом каналов."""

    values = list(range(frame_samples * channels))
    return struct.pack("<" + "h" * len(values), *values)


def test_downmix_to_mono() -> None:
    """Проверяем, что усреднение двух каналов работает корректно."""

    stereo = struct.pack("<hhhh", 1000, -1000, 2000, -2000)
    mono = downmix_to_mono(stereo, 2)
    # Усреднение пары (1000, -1000) даёт 0, аналогично для второй пары.
    assert mono == struct.pack("<hh", 0, 0)


def test_decode_frame_fields() -> None:
    """Распаковка бинарного PCM присваивает корректные метаданные."""

    stream = RobotAudioStream("ws://127.0.0.1:8765/", expected_channels=2)
    pcm_payload = _build_pcm(frame_samples=4, channels=2)
    frame = stream._decode_frame(pcm_payload)
    assert frame is not None
    assert frame.sequence == 1
    # Проверяем, что моно-буфер соответствует усреднению каналов.
    expected_mono = downmix_to_mono(pcm_payload, 2)
    assert frame.pcm_mono == expected_mono
    assert frame.localization_enabled is False
    assert frame.channels == 2
    assert frame.sample_rate == stream.sample_rate
    assert frame.frame_samples == len(expected_mono) // 2


def test_websocket_server_receives_audio() -> None:
    """Интеграционный тест: сервер принимает PCM и обновляет метаданные."""

    async def _runner() -> RobotAudioStream:
        stream = RobotAudioStream("ws://127.0.0.1:0/robot", queue_max=2)
        await stream.start()
        assert stream._server is not None
        port = stream._server.sockets[0].getsockname()[1]
        pcm_payload = _build_pcm(frame_samples=4, channels=2)

        async with websockets.connect(f"ws://127.0.0.1:{port}/robot") as ws:
            meta = json.dumps(
                {
                    "type": "mic_chunk",
                    "sample_rate": 16_000,
                    "sample_format": "s16le",
                    "channels": 2,
                }
            )
            await ws.send(meta)
            await ws.send(pcm_payload)

        frame = await asyncio.wait_for(stream.read(), timeout=1.0)
        assert frame.sequence == 1
        assert frame.sample_rate == 16_000
        assert frame.channels == 2
        assert frame.pcm_mono == downmix_to_mono(pcm_payload, 2)
        # После остановки чтение должно вызвать исключение.
        assert stream.stop() is True
        await asyncio.sleep(0.05)
        with pytest.raises(RobotStreamClosed):
            await asyncio.wait_for(stream.read(), timeout=1.0)
        return stream

    asyncio.run(_runner())


def test_websocket_server_sends_tts_to_robot() -> None:
    """Проверяем, что отправка озвучки идёт в формате audio_start/PCM/audio_end."""

    async def _runner() -> list[object]:
        stream = RobotAudioStream("ws://127.0.0.1:0/robot", queue_max=2)
        await stream.start()
        assert stream._server is not None
        port = stream._server.sockets[0].getsockname()[1]

        async with websockets.connect(f"ws://127.0.0.1:{port}/robot") as ws:
            pcm = struct.pack("<hh", 1200, -1200)
            stream.send_tts(
                pcm,
                16_000,
                text="привет",
                preset="neutral",
                chunk_index=1,
                chunks_total=1,
                volume=1.0,
            )
            first = await asyncio.wait_for(ws.recv(), timeout=1.0)
            second = await asyncio.wait_for(ws.recv(), timeout=1.0)
            third = await asyncio.wait_for(ws.recv(), timeout=1.0)
            return [first, second, third]

    messages = asyncio.run(_runner())

    start_msg, payload, end_msg = messages
    assert isinstance(start_msg, str)
    start = json.loads(start_msg)
    assert start["type"] == "audio_start"
    assert start["sample_rate"] == 16_000
    assert start["sample_format"] == "s16le"
    assert start["channels"] == 1

    assert isinstance(payload, (bytes, bytearray))
    assert payload == struct.pack("<hh", 1200, -1200)

    assert isinstance(end_msg, str)
    assert json.loads(end_msg)["type"] == "audio_end"


def test_websocket_server_sends_effect_to_robot() -> None:
    """Эффекты эмоций тоже должны доходить через новый протокол."""

    async def _runner() -> list[object]:
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
            first = await asyncio.wait_for(ws.recv(), timeout=1.0)
            second = await asyncio.wait_for(ws.recv(), timeout=1.0)
            third = await asyncio.wait_for(ws.recv(), timeout=1.0)
            return [first, second, third]

    messages = asyncio.run(_runner())

    start_msg, payload, end_msg = messages
    start = json.loads(start_msg)
    assert start["type"] == "audio_start"
    assert start["kind"] == "emotion"
    assert start["sample_rate"] == 22_050

    assert isinstance(payload, (bytes, bytearray))
    assert payload == struct.pack("<hhhh", 500, -500, 1000, -1000)

    assert json.loads(end_msg)["type"] == "audio_end"


def test_chunking_does_not_exceed_frame_limit() -> None:
    """Крупные PCM-буферы дробятся на безопасные кадры для ESP32."""

    stream = RobotAudioStream(
        "ws://127.0.0.1:8765/",
        max_outgoing_frame=4,
    )
    # Притворяемся, что сервер запущен: переопределяем очередь отправки
    # на простые накопители и выставляем loop, чтобы обходной путь не
    # блокировался проверками на None.
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    stream._loop = loop

    sent_binary: list[bytes] = []
    sent_json: list[dict] = []

    def _fake_broadcast_binary(payload: bytes, *, purpose: str) -> None:
        sent_binary.append(payload)

    def _fake_broadcast_json(payload: dict, *, purpose: str) -> None:
        sent_json.append(payload)

    stream._broadcast_binary = _fake_broadcast_binary  # type: ignore[method-assign]
    stream._broadcast_json = _fake_broadcast_json  # type: ignore[method-assign]

    pcm = b"0123456789"  # 10 байт превратятся в 3 фрейма по 4,4,2
    stream.send_tts(
        pcm,
        16_000,
        text="chunk-test",
        preset="neutral",
        chunk_index=1,
        chunks_total=1,
        volume=1.0,
    )

    assert len(sent_json) == 2  # audio_start и audio_end
    assert sent_json[0]["type"] == "audio_start"
    assert sent_json[-1]["type"] == "audio_end"

    assert [len(piece) for piece in sent_binary] == [4, 4, 2]
    assert b"".join(sent_binary) == pcm
    # Проверяем, что split_total в логах вычислился корректно.
    assert math.ceil(len(pcm) / stream._max_outgoing_frame) == 3

    loop.close()
    asyncio.set_event_loop(None)
