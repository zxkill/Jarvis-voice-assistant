"""Тесты приёмника аудиопотока робота."""

from __future__ import annotations

import asyncio
import json
import struct

import pytest
import websockets
from websockets.exceptions import ConnectionClosedError
from websockets.frames import Close

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
                preset="neutral",
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


def test_tts_is_split_into_small_frames() -> None:
    """Крупный PCM должен резаться на небольшие фреймы, чтобы не валить ESP32."""

    async def _runner() -> list[bytes]:
        stream = RobotAudioStream("ws://127.0.0.1:0/robot", queue_max=2)
        await stream.start()
        # Создаём поддельную очередь отправки, имитирующую подключение робота.
        queue: asyncio.Queue[bytes] = asyncio.Queue()
        stream._send_queues.add(queue)

        # Генерируем ~100 мс моно PCM (1600 сэмплов при 16 кГц).
        pcm = struct.pack("<" + "h" * 1600, *range(1600))
        # Просим делить звук на фреймы по 256 сэмплов.
        stream.send_tts(
            pcm,
            16_000,
            text="тест",  # noqa: PIE798 — важно видеть в логах
            preset="neutral",
            chunk_index=1,
            chunks_total=1,
            volume=1.0,
            frame_samples=256,
        )

        await asyncio.sleep(0.05)
        collected: list[bytes] = []
        while not queue.empty():
            collected.append(queue.get_nowait())
        return collected

    payloads = asyncio.run(_runner())

    # 1600 / 256 = 6.25 → ожидаем 7 фреймов, чтобы последний включал остаток.
    assert len(payloads) == 7
    for payload in payloads:
        header = _PLAYBACK_HEADER.unpack_from(payload)
        assert header[0] == b"AP"
        # Убеждаемся, что размер pcm_bytes не превышает порог и совпадает с frameSamples.
        pcm_bytes = header[9]
        frame_samples = header[8]
        assert pcm_bytes == frame_samples * 2  # mono => 2 байта на сэмпл
        assert frame_samples <= 256


def test_tts_respects_payload_limit_and_throttles_warning() -> None:
    """Лимит размера полезной нагрузки предотвращает код 1009, а предупреждения не спамят логи."""

    async def _runner() -> tuple[list[int], float, float, int]:
        # Задаём лимит полезной нагрузки: заголовок + 64 байта PCM (32 сэмпла моно).
        payload_limit = _PLAYBACK_HEADER.size + 64
        stream = RobotAudioStream(
            "ws://127.0.0.1:0/robot",
            queue_max=2,
            max_playback_payload=payload_limit,
        )
        await stream.start()
        # Подключений нет, но имитируем очередь отправки, как если бы робот принял handshake.
        fake_queue: asyncio.Queue[bytes] = asyncio.Queue()
        stream._send_queues.add(fake_queue)

        # Поддельный клиент: отправляем в очередь напрямую, подключений нет.
        pcm = struct.pack("<" + "h" * 200, *range(200))  # 400 байт PCM16
        stream.send_tts(
            pcm,
            16_000,
            text="ограничение",  # noqa: PIE798
            preset="neutral",
            chunk_index=1,
            chunks_total=1,
            volume=1.0,
        )
        await asyncio.sleep(0.05)
        first_warning_ts = stream._last_no_client_warning

        # Повторный вызов сразу после первого не должен обновить таймстамп из-за троттлинга.
        stream.send_tts(
            pcm,
            16_000,
            text="повтор",  # noqa: PIE798
            preset="neutral",
            chunk_index=1,
            chunks_total=1,
            volume=1.0,
        )
        await asyncio.sleep(0.05)
        second_warning_ts = stream._last_no_client_warning

        # Собираем кадры, которые попали в очередь отправки: они должны укладываться в лимит.
        enqueued_sizes: list[int] = []
        while not fake_queue.empty():
            enqueued_sizes.append(len(fake_queue.get_nowait()))

        return enqueued_sizes, first_warning_ts, second_warning_ts, stream._max_playback_payload

    sizes, first_ts, second_ts, effective_limit = asyncio.run(_runner())

    # Все кадры должны быть меньше заданного лимита (заголовок + 64 байта).
    assert sizes  # убедимся, что кадры вообще формируются
    assert all(size <= effective_limit for size in sizes)
    # Предупреждение не должно срабатывать чаще раза в секунду.
    assert second_ts == first_ts


class _DummyClosedWebSocket:
    """Фиктивный WebSocket, имитирующий разрыв соединения."""

    async def send(self, payload: bytes) -> None:  # pragma: no cover - поведение задаётся тестом
        # Возвращаем Close-кадр, как если бы его прислал робот, чтобы код ошибки был доступен.
        raise ConnectionClosedError(Close(1006, "connection reset"), None)


def test_send_loop_survives_connection_reset() -> None:
    """При разрыве соединения цикл отправки должен завершаться без исключений."""

    async def _runner() -> None:
        stream = RobotAudioStream("ws://127.0.0.1:0/robot")
        queue: asyncio.Queue[bytes] = asyncio.Queue()
        queue.put_nowait(b"pcm")

        # Убеждаемся, что ConnectionClosedError обрабатывается и не утекает наружу.
        await stream._send_loop(_DummyClosedWebSocket(), queue, peer="test-peer")

    asyncio.run(_runner())
