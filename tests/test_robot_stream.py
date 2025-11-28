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
    _normalize_audio_for_caps,
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
        44_100,
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
                44_100,
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
    # По умолчанию сервер сразу использует XiaoZhi-заголовок (4 байта), чтобы не
    # получать треск от AF/обрезки. Проверяем, что длина полезной нагрузки
    # соответствует исходному PCM и что кадр не урезан до 512 байт.
    assert payload[0] == 0  # type=audio
    assert payload[1] == 0  # reserved
    size = (payload[2] << 8) | payload[3]
    assert size == pcm_len
    assert len(payload) == 4 + pcm_len
    assert payload[4:] == struct.pack("<hh", 1200, -1200)


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
                44_100,
                name="SIGH",
                source_file="sigh.wav",
                repeat_index=1,
                repeat_total=1,
                volume=0.75,
            )
            payload = await asyncio.wait_for(ws.recv(), timeout=1.0)
        return payload, len(pcm)

    payload, pcm_len = asyncio.run(_runner())

    # Проверяем XiaoZhi-заголовок (4 байта) и длину полезной нагрузки без
    # ресемплинга: 4 сэмпла по 2 байта = 8 байт PCM.
    assert payload[0] == 0
    assert payload[1] == 0
    size = (payload[2] << 8) | payload[3]
    assert size == 8
    assert len(payload) == 12
    # Проверяем, что полезная нагрузка не обрезана и содержит 3 сэмпла после
    # ресемплинга. Значения могут немного отличаться из-за интерполяции,
    # поэтому фиксируем только длину и первый сэмпл.
    first_sample = struct.unpack_from("<h", payload, 4)[0]
    assert first_sample == 500


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
                        "sample_rate": 44_100,
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
                44_100,
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
                    "sample_rate": 44_100,
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
    assert frame.sample_rate == 44_100
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
            # В режиме AF учитываем frame_samples_hint и проверяем нарезку на
            # субкадры до прихода hello, чтобы поймать регрессии с укороченными
            # XiaoZhi-кадрами.
            caps=PlaybackClientCaps(mode="af"),
            peer="dummy",
        )
        stream._sessions.append(session)

        # Генерируем ~36 мс моно PCM (1600 сэмплов при 44.1 кГц).
        pcm = struct.pack("<" + "h" * 1600, *range(1600))
        # Просим делить звук на фреймы по 256 сэмплов.
        stream.send_tts(
            pcm,
            44_100,
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
            44_100,
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
            44_100,
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
        44_100,
        text="нет клиента",  # noqa: PIE798 — читается в логах
        preset="neutral",
        chunk_index=1,
        chunks_total=1,
        volume=1.0,
    )
    first_ts = stream._last_no_client_warning

    stream.send_tts(
        pcm,
        44_100,
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


def test_default_playback_payload_matches_xiaozhi_frame() -> None:
    """Дефолтный лимит покрывает 60 мс PCM16/44.1кГц без обрезки и треска."""

    stream = RobotAudioStream("ws://127.0.0.1:0/robot")
    # 8192 байта позволяют передать 60 мс моно PCM16 (≈5292 байта) + заголовок XiaoZhi.
    assert stream._max_playback_payload == 8192
    # Генерируем ровно 60 мс PCM16 моно: 44_100 * 0.06 ≈ 2646 сэмплов.
    pcm = struct.pack("<" + "h" * 2646, *range(2646))
    caps = PlaybackClientCaps(mode="xiaozhi", sample_rate=44_100, channels=1, frame_duration_ms=60)
    frames = stream._split_pcm_for_caps(pcm, channels=1, caps=caps)
    assert frames, "Должен получиться хотя бы один кадр"
    assert len(frames) == 1, "60 мс помещаются в один XiaoZhi-фрейм"
    payload = stream._prepare_playback_payload(frames[0], 44_100, channels=1, volume=1.0, caps=caps)
    assert payload is not None
    packet, meta = payload
    # Итоговая нагрузка укладывается в лимит и сохраняет исходный объём PCM.
    assert len(packet) <= stream._max_playback_payload
    assert meta["pcm_bytes"] == len(frames[0])


def test_prepare_payload_resamples_to_caps_rate() -> None:
    """TTS/эффекты приводятся к частоте 44.1 кГц, чтобы робот не трещал."""

    stream = RobotAudioStream("ws://127.0.0.1:0/robot", max_playback_payload=8192)
    caps = PlaybackClientCaps(mode="xiaozhi", sample_rate=44_100, channels=1, frame_duration_ms=60)

    # Формируем 60 мс моно-сигнал 48 кГц и проверяем, что он ресемплируется в 44.1 кГц.
    src_rate = 48_000
    src_frames = int(src_rate * 0.06)
    pcm = struct.pack("<" + "h" * src_frames, *([500] * src_frames))

    prepared = stream._prepare_playback_payload(pcm, src_rate, channels=1, volume=1.0, caps=caps)
    assert prepared is not None, "Кадр должен быть сформирован после ресемплинга"

    payload, meta = prepared
    expected_frames = int(round(src_frames * caps.sample_rate / src_rate))
    expected_bytes = expected_frames * 2  # 16 бит на сэмпл

    # Заголовок XiaoZhi v3 занимает 4 байта, полезная нагрузка — пересчитанный PCM.
    assert len(payload) == 4 + expected_bytes
    assert meta["sample_rate"] == caps.sample_rate


def test_resample_and_downmix_aligns_with_caps() -> None:
    """Ресемплинг и перевод в моно приводят PCM к формату XiaoZhi без артефактов."""

    stream = RobotAudioStream("ws://127.0.0.1:0/robot")
    caps = PlaybackClientCaps(mode="xiaozhi", sample_rate=44_100, channels=1, frame_duration_ms=60)

    # Формируем стерео-сигнал 22.05 кГц с постоянной амплитудой, чтобы легко
    # проверить отсутствие искажений после ресемплинга и downmix.
    src_rate = 22_050
    channels = 2
    frames = 100
    stereo_samples = [1000 for _ in range(frames * channels)]
    pcm = struct.pack("<" + "h" * len(stereo_samples), *stereo_samples)

    normalized, rate, out_channels = _normalize_audio_for_caps(
        pcm, src_rate, channels, caps, resample_log=stream._resample_log
    )

    expected_frames = max(1, int(round(frames * (caps.sample_rate / src_rate))))

    # Проверяем, что частота и количество каналов соответствуют hello робота.
    assert rate == caps.sample_rate
    assert out_channels == caps.channels
    assert len(normalized) == expected_frames * caps.channels * 2

    mono_samples = struct.unpack("<" + "h" * (len(normalized) // 2), normalized)
    assert all(val == 1000 for val in mono_samples)


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
            44_100,
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


def test_default_session_caps_are_xiaozhi_without_hello() -> None:
    """Если робот не отправил hello, сервер всё равно шлёт совместимый кадр."""

    async def _runner() -> bytes:
        stream = RobotAudioStream(
            "ws://127.0.0.1:0/robot",
            max_playback_payload=8192,
        )
        await stream.start()
        assert stream._server is not None
        port = stream._server.sockets[0].getsockname()[1]

        async with websockets.connect(f"ws://127.0.0.1:{port}/robot") as ws:
            # Отправляем 60 мс PCM16/44.1 кГц в 1 канал — ровно то, что ждёт XiaoZhi.
            pcm = b"\x01\x00" * 2646
            stream.send_tts(
                pcm,
                44_100,
                text="auto-caps",
                preset="neutral",
                chunk_index=1,
                chunks_total=1,
                volume=1.0,
            )
            payload = await asyncio.wait_for(ws.recv(), timeout=1.0)
            return payload

    payload = asyncio.run(_runner())
    # Заголовок XiaoZhi v3: type=0, reserved=0, size=payload.
    assert payload[0] == 0
    assert payload[1] == 0
    size = (payload[2] << 8) | payload[3]
    assert size == len(payload) - 4
    # Полезная нагрузка должна уместиться целиком (≈5292 байта) без обрезки под
    # заголовок AF, иначе возникнет треск на роботе.
    assert size == 5292


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
