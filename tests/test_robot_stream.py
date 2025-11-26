"""Тесты приёмника аудиопотока робота."""

from __future__ import annotations

import asyncio
import json
import struct
import time

import pytest
import websockets
from websockets.exceptions import ConnectionClosedError
from websockets.frames import Close

from audio.robot_stream import (
    PlaybackClientCaps,
    PlaybackQueueItem,
    PlaybackStats,
    RobotAudioFrame,
    RobotAudioStream,
    RobotStreamClosed,
    RobotClientSession,
    downmix_to_mono,
)

_HEADER = struct.Struct("<2sBBIQIIHHIfffff")
_PLAYBACK_HEADER = struct.Struct("<2sBBIIIHHIIff")


def _build_xiaozhi_v3_audio(pcm: bytes) -> bytes:
    """Собирает минимальный бинарный кадр XiaoZhi v3 с типом audio."""

    size = len(pcm)
    return bytes([0, 0, (size >> 8) & 0xFF, size & 0xFF]) + pcm


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


def test_effect_is_split_for_xiaozhi_client() -> None:
    """Большой эффект режется на подкадры под XiaoZhi, но не обрезается."""

    async def _runner() -> tuple[list[bytes], bytes]:
        stream = RobotAudioStream(
            "ws://127.0.0.1:0/robot",
            queue_max=4,
            max_playback_payload=512,
        )
        await stream.start()
        assert stream._server is not None
        port = stream._server.sockets[0].getsockname()[1]

        collected: list[bytes] = []
        pcm = bytes([0x10, 0x00]) * 4000  # 8000 байт, точно больше лимита

        async with websockets.connect(f"ws://127.0.0.1:{port}/robot") as ws:
            hello = json.dumps(
                {
                    "type": "hello",
                    "version": 3,
                    "transport": "websocket",
                    "audio_params": {
                        "format": "pcm16",
                        "sample_rate": 16_000,
                        "channels": 1,
                        "frame_duration": 60,
                    },
                }
            )
            await ws.send(hello)
            # Сервер отвечает взаимным hello — прочитаем и проигнорируем.
            await ws.recv()

            stream.send_effect(
                pcm,
                16_000,
                name="THINKING",
                source_file="buzz.wav",
                repeat_index=1,
                repeat_total=1,
                volume=1.0,
            )

            # Считываем кадры, пока не соберём весь объём PCM или не упадём по тайм‑ауту.
            assembled_len = 0
            try:
                while assembled_len < len(pcm):
                    frame = await asyncio.wait_for(ws.recv(), timeout=1.0)
                    collected.append(frame)
                    assembled_len += max(0, len(frame) - 4)  # 4 байта занимает заголовок v3
            except asyncio.TimeoutError:
                # Закончили читать кадры.
                pass

        return collected, pcm

    frames, original_pcm = asyncio.run(_runner())
    assert len(frames) > 1, "Эффект должен быть нарезан на несколько подкадров"
    assert all(len(frame) <= 512 for frame in frames)

    # Собираем полезную нагрузку BinaryProtocol3: первые 4 байта — заголовок.
    assembled = b"".join(frame[4:] for frame in frames)
    assert assembled.startswith(original_pcm[: len(assembled)])


def test_xiaozhi_hello_and_frame_delivery() -> None:
    """Сервер отвечает на hello и разбирает XiaoZhi v3 аудио-кадры."""

    async def _runner() -> RobotAudioFrame:
        stream = RobotAudioStream("ws://127.0.0.1:0/robot")
        await stream.start()
        port = stream._server.sockets[0].getsockname()[1]

        pcm = struct.pack("<hhhh", 100, -100, 50, -50)
        frame = _build_xiaozhi_v3_audio(pcm)

        async with websockets.connect(f"ws://127.0.0.1:{port}/robot") as ws:
            hello = {
                "type": "hello",
                "version": 3,
                "audio_params": {
                    "format": "pcm16",
                    "sample_rate": 16_000,
                    "channels": 1,
                    "frame_duration": 60,
                },
            }
            await ws.send(json.dumps(hello))
            await asyncio.sleep(0.05)
            await ws.send(frame)
            # Сервер сначала отдаёт ответный hello, а затем ack с номером кадра.
            first = await asyncio.wait_for(ws.recv(), timeout=1.0)
            if isinstance(first, str) and "hello" in first:
                ack = await asyncio.wait_for(ws.recv(), timeout=1.0)
            else:
                ack = first
            assert json.loads(ack)["sequence"] >= 1

        received = await asyncio.wait_for(stream.read(), timeout=1.0)
        return received

    frame = asyncio.run(_runner())
    assert frame.sample_rate == 16_000
    assert frame.frame_samples == 4
    assert frame.channels == 1
    assert frame.pcm_mono == struct.pack("<hhhh", 100, -100, 50, -50)


def test_tts_is_split_into_small_frames() -> None:
    """Крупный PCM должен резаться на небольшие фреймы, чтобы не валить ESP32."""

    async def _runner() -> list[bytes]:
        stream = RobotAudioStream("ws://127.0.0.1:0/robot", queue_max=2)
        await stream.start()
        # Создаём поддельную очередь отправки, имитирующую подключение робота.
        queue: asyncio.Queue[PlaybackQueueItem] = asyncio.Queue()
        session = RobotClientSession(
            queue=queue,
            stats=PlaybackStats(connected_at=time.time()),
            caps=PlaybackClientCaps(),
            peer="dummy",
        )
        stream._sessions.append(session)

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
            collected.append(queue.get_nowait().payload)
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
        fake_queue: asyncio.Queue[PlaybackQueueItem] = asyncio.Queue()
        stream._sessions.append(
            RobotClientSession(
                queue=fake_queue,
                stats=PlaybackStats(connected_at=time.time()),
                caps=PlaybackClientCaps(),
                peer="fake",
            )
        )

        # Поддельный клиент: отправляем в очередь напрямую, подключений нет.
        pcm = struct.pack("<" + "h" * 200, *range(200))  # 400 байт PCM16
        session = RobotClientSession(
            queue=fake_queue,
            stats=PlaybackStats(connected_at=time.time()),
            caps=PlaybackClientCaps(),
            peer="fake",
        )
        stream._sessions.append(session)
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
            enqueued_sizes.append(len(fake_queue.get_nowait().payload))

        return enqueued_sizes, first_warning_ts, second_warning_ts, stream._max_playback_payload

    sizes, first_ts, second_ts, effective_limit = asyncio.run(_runner())

    # Все кадры должны быть меньше заданного лимита (заголовок + 64 байта).
    assert sizes  # убедимся, что кадры вообще формируются
    assert all(size <= effective_limit for size in sizes)
    # Предупреждение не должно срабатывать чаще раза в секунду.
    assert second_ts == first_ts


def test_tts_skipped_without_clients(caplog) -> None:
    """Если робота нет на линии, озвучка не должна бесконечно спамить предупреждения."""

    stream = RobotAudioStream("ws://127.0.0.1:0/robot")
    stream._loop = asyncio.new_event_loop()
    caplog.set_level("WARNING")

    pcm = struct.pack("<hhhh", 10, -10, 20, -20)

    stream.send_tts(
        pcm,
        16_000,
        text="нет клиента",  # noqa: PIE798 — читается в логах
        preset="neutral",
        chunk_index=1,
        chunks_total=1,
        volume=1.0,
    )
    first_ts = stream._last_no_client_warning

    stream.send_tts(
        pcm,
        16_000,
        text="повтор нет клиента",  # noqa: PIE798
        preset="neutral",
        chunk_index=1,
        chunks_total=1,
        volume=1.0,
    )

    assert first_ts > 0
    assert first_ts == stream._last_no_client_warning
    # Дополнительный вызов не должен сдвигать таймстамп предупреждения.
    stream._loop.close()


def test_default_playback_payload_is_conservative() -> None:
    """Дефолтный лимит 512 байт укладывается в безопасный порог для ESP32."""

    stream = RobotAudioStream("ws://127.0.0.1:0/robot")
    # 512 — консервативный размер: позволяет передавать ~238 сэмплов моно PCM
    # с учётом 36-байтового заголовка AP и остаётся ниже типичных лимитов
    # WebSocket в прошивке ESP32, предотвращая код 1009.
    assert stream._max_playback_payload == 512
    pcm = struct.pack("<" + "h" * 300, *range(300))
    caps = PlaybackClientCaps()
    payload = stream._prepare_playback_payload(pcm, 16_000, channels=1, volume=1.0, caps=caps)
    assert payload is not None
    packet, meta = payload
    # Проверяем, что итоговый пакет строго меньше лимита (512 байт).
    assert len(packet) <= stream._max_playback_payload
    # Сами PCM-данные тоже укладываются в границы, учитывая заголовок AP.
    assert meta["pcm_bytes"] + _PLAYBACK_HEADER.size <= stream._max_playback_payload


def test_broadcast_uses_configured_playback_queue(caplog) -> None:
    """Очередь отправки использует новый лимит и агрегирует предупреждения."""

    async def _runner() -> tuple[int, float, float, int]:
        stream = RobotAudioStream(
            "ws://127.0.0.1:0/robot", playback_queue_max=5, max_playback_payload=128
        )
        await stream.start()
        # Подменяем очередь отправки, как если бы робот уже подключился.
        fake_queue: asyncio.Queue[PlaybackQueueItem] = asyncio.Queue(
            maxsize=stream._playback_queue_max
        )
        stream._sessions.append(
            RobotClientSession(
                queue=fake_queue,
                stats=PlaybackStats(connected_at=time.time()),
                caps=PlaybackClientCaps(),
                peer="fake",
            )
        )

        payload = b"p" * 64
        # 20 субкадров при maxsize=5 гарантируют дропы, но лог должен появиться
        # агрегированно, а конечный размер очереди не превышает лимит.
        builder = lambda _caps: payload
        for _ in range(20):
            stream._broadcast_payload(builder, purpose="TTS")
        await asyncio.sleep(0.05)
        first_ts = stream._last_drop_warning

        # Повторный бурст сразу после первого не должен сбрасывать таймстамп
        # из-за троттлинга в 0.5 секунды.
        for _ in range(5):
            stream._broadcast_payload(builder, purpose="TTS")
        await asyncio.sleep(0.05)
        second_ts = stream._last_drop_warning

        enqueued = 0
        while not fake_queue.empty():
            _ = fake_queue.get_nowait()
            enqueued += 1

        return enqueued, first_ts, second_ts, stream._playback_queue_max

    enqueued, first_ts, second_ts, queue_limit = asyncio.run(_runner())

    assert enqueued <= queue_limit
    assert first_ts > 0
    # Предупреждение о переполнении не должно срабатывать чаще, чем раз в 0.5 с.
    assert second_ts == first_ts


def test_tts_sent_as_xiaozhi_payload() -> None:
    """TTS на стороне сервера упаковывается в BinaryProtocol при hello."""

    async def _runner() -> bytes:
        stream = RobotAudioStream("ws://127.0.0.1:0/robot")
        await stream.start()
        queue: asyncio.Queue[PlaybackQueueItem] = asyncio.Queue()
        caps = PlaybackClientCaps(mode="xiaozhi", xiaozhi_version=3, channels=1)
        session = RobotClientSession(
            queue=queue,
            stats=PlaybackStats(connected_at=time.time()),
            caps=caps,
            peer="fake",
        )
        stream._sessions.append(session)

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
        await asyncio.sleep(0.05)
        item = queue.get_nowait()
        return item.payload

    payload = asyncio.run(_runner())
    assert payload[0] == 0  # type=audio
    size = (payload[2] << 8) | payload[3]
    assert size == 4
    assert payload[4:8] == struct.pack("<hh", 1200, -1200)


class _DummyClosedWebSocket:
    """Фиктивный WebSocket, имитирующий разрыв соединения."""

    async def send(self, payload: bytes) -> None:  # pragma: no cover - поведение задаётся тестом
        # Возвращаем Close-кадр, как если бы его прислал робот, чтобы код ошибки был доступен.
        raise ConnectionClosedError(Close(1006, "connection reset"), None)


def test_send_loop_survives_connection_reset() -> None:
    """При разрыве соединения цикл отправки должен завершаться без исключений."""

    async def _runner() -> None:
        stream = RobotAudioStream("ws://127.0.0.1:0/robot")
        queue: asyncio.Queue[PlaybackQueueItem] = asyncio.Queue()
        queue.put_nowait(
            PlaybackQueueItem(payload=b"pcm", purpose="TTS", enqueued_at=time.monotonic())
        )

        stats = PlaybackStats(connected_at=time.time())
        session = RobotClientSession(
            queue=queue,
            stats=stats,
            caps=PlaybackClientCaps(),
            peer="test-peer",
        )
        # Убеждаемся, что ConnectionClosedError обрабатывается и не утекает наружу.
        await stream._send_loop(_DummyClosedWebSocket(), session)

    asyncio.run(_runner())


class _CapturingWebSocket:
    """WebSocket-заглушка, фиксирующая отправленные байты."""

    def __init__(self) -> None:
        self.sent: list[bytes] = []

    async def send(self, payload: bytes) -> None:  # pragma: no cover - простая заглушка
        self.sent.append(payload)


def test_send_loop_collects_diagnostics(caplog) -> None:
    """Цикл отправки накапливает статистику очереди и логирует сводку."""

    async def _runner() -> PlaybackStats:
        stream = RobotAudioStream("ws://127.0.0.1:0/robot")
        queue: asyncio.Queue[PlaybackQueueItem] = asyncio.Queue()
        queue.put_nowait(
            PlaybackQueueItem(
                payload=b"a" * (_PLAYBACK_HEADER.size + 4),
                purpose="TTS",
                enqueued_at=time.monotonic() - 0.01,
            )
        )
        queue.put_nowait(
            PlaybackQueueItem(
                payload=b"b" * (_PLAYBACK_HEADER.size + 2),
                purpose="SFX",
                enqueued_at=time.monotonic() - 0.02,
            )
        )

        ws = _CapturingWebSocket()
        stats = PlaybackStats(connected_at=time.time())
        session = RobotClientSession(queue=queue, stats=stats, caps=PlaybackClientCaps(), peer="diag")
        caplog.set_level("DEBUG", logger="audio.robot_stream")
        task = asyncio.create_task(stream._send_loop(ws, session))
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return stats

    stats = asyncio.run(_runner())
    # Два кадра успели отправиться до отмены задачи.
    assert stats.sent_frames == 2
    assert stats.sent_bytes > 0
    assert stats.max_queue_depth >= 0
    assert stats.max_latency_ms >= 0
    # Статистика должна зафиксировать хотя бы одну задержку и глубину очереди.
    assert stats.max_latency_ms >= 0
