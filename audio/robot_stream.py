"""WebSocket-аудиоканал робота Jarvis.

Единый протокол v1:

Вход ESP32 -> сервер:
    бинарный кадр ``AF v1`` + PCM16 interleaved.
    Для совместимости сервер также принимает старый raw PCM16 без заголовка.

Выход сервер -> ESP32:
    JSON ``audio_start`` -> бинарные raw PCM16 mono чанки -> JSON ``audio_end``.

ESP32 не должен разбирать сложные заголовки на входящем TTS: только управляющие
JSON-сообщения и последовательность PCM-байтов между ними.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import math
import struct
import time
from array import array
from collections import deque
from typing import Deque, Iterable, Set
from urllib.parse import urlparse

import websockets
from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK
from websockets.legacy.server import Serve, WebSocketServerProtocol, serve

from core.logging_json import configure_logging

# Последний IP ESP32, который подключался к аудиоканалу.
# BodyController использует это как fallback, чтобы не заставлять руками
# прописывать адрес робота после каждого DHCP-переезда.
_LAST_ROBOT_HOST: str | None = None
_LAST_ROBOT_PEER: str | None = None


def get_last_robot_host() -> str | None:
    """Возвращает последний известный IP/host ESP32 из WebSocket-аудиоканала."""

    return _LAST_ROBOT_HOST


def get_last_robot_base_url() -> str | None:
    """Возвращает HTTP base_url панели ESP32, если робот уже подключался."""

    if not _LAST_ROBOT_HOST:
        return None
    return f"http://{_LAST_ROBOT_HOST}"


def get_last_robot_peer() -> str | None:
    """Возвращает peer вида host:port для диагностики."""

    return _LAST_ROBOT_PEER


@dataclasses.dataclass(slots=True)
class RobotAudioFrame:
    """Один аудиокадр, полученный от робота."""

    sequence: int
    timestamp_us: int
    sample_rate: int
    frame_samples: int
    channels: int
    sample_bits: int
    pcm_stereo: bytes
    pcm_mono: bytes
    rms_left: float
    rms_right: float
    mic_spacing_m: float
    direction_deg: float
    confidence: float
    localization_enabled: bool


class RobotStreamClosed(Exception):
    """Поток остановлен или WebSocket-сервер закрыт."""


@dataclasses.dataclass(slots=True)
class _AfHeader:
    sequence: int
    timestamp_us: int
    sample_rate: int
    frame_samples: int
    channels: int
    sample_bits: int
    pcm_bytes: int
    rms_left: float
    rms_right: float
    mic_spacing_m: float
    direction_deg: float
    confidence: float
    localization_enabled: bool
    payload_offset: int


class RobotAudioStream:
    """WebSocket-сервер для аудио робота.

    Сервер принимает входящий звук от ESP32 и ретранслирует TTS/эмоции обратно
    в то же подключение. Основная логика приложения читает кадры через ``read``.
    """

    _SENTINEL = object()
    _AF_HEADER_SIZE = 52

    def __init__(
        self,
        endpoint: str,
        queue_max: int = 200,
        *,
        expected_sample_rate: int = 16_000,
        expected_channels: int = 2,
        subprotocol: str | None = None,
        authorization: str | None = None,
        ping_interval: float | None = 10.0,
        ping_timeout: float | None = 5.0,
        max_outgoing_frame: int = 4096,
        send_queue_max: int = 200,
        stt_channel: str = "best",
        tts_preroll_ms: int = 120,
        tts_stream_pace: float = 0.96,
        tts_initial_burst_ms: int = 650,
    ) -> None:
        self._parsed = urlparse(endpoint)
        if self._parsed.scheme not in {"ws", "wss"}:
            raise ValueError("endpoint должен начинаться с ws:// или wss://")

        self._host = self._parsed.hostname or "0.0.0.0"
        self._port = self._parsed.port if self._parsed.port is not None else 8765
        self._path = self._parsed.path or "/robot"
        self._accepted_paths = {self._path}
        # Переходный режим: старые прошивки подключались к корню. Это можно
        # удалить после перепрошивки робота на /robot.
        if self._path == "/robot":
            self._accepted_paths.add("/")

        self._subprotocol = subprotocol
        self._authorization = authorization
        self._ping_interval = ping_interval
        self._ping_timeout = ping_timeout
        self._max_outgoing_frame = max(1, max_outgoing_frame)
        # Аудио нельзя вываливать в ESP32 мгновенно: маленький бортовой буфер
        # переполняется, и из речи выпадают слоги/цифры. Очередь делаем больше,
        # а отправку бинарных PCM-чанков ниже слегка ограничиваем по времени.
        self._send_queue_max = max(400, send_queue_max)
        # После стартового предбуфера TTS идёт почти в реальном времени,
        # но чуть быстрее него. Это создаёт небольшой запас в очереди ESP32
        # и убирает лёгкие underrun-заикания в середине длинных фраз.
        # Значение < 1.0 быстрее реального времени, > 1.0 медленнее.
        self._outgoing_audio_pace = max(0.90, min(1.15, float(tts_stream_pace)))
        # Стартовый запас: первые несколько сотен миллисекунд PCM отправляем
        # быстрее, чтобы у ESP32 был буфер не только на начало фразы, но и на
        # небольшие Wi‑Fi/планировочные задержки дальше по длинному ответу.
        self._tts_initial_burst_ms = max(0, int(tts_initial_burst_ms))
        # Короткая тишина перед первым PCM-фреймом даёт MAX98357A/I2S время
        # стабильно открыть поток и защищает от съедания первых миллисекунд речи.
        self._tts_preroll_ms = max(0, int(tts_preroll_ms))
        self._stt_channel = (stt_channel or "best").strip().lower()
        if self._stt_channel not in {"mix", "left", "right", "best"}:
            self._stt_channel = "best"

        self._queue: asyncio.Queue[RobotAudioFrame | object] = asyncio.Queue(
            maxsize=max(1, queue_max)
        )
        self._expected_sample_rate = expected_sample_rate
        self._expected_channels = expected_channels
        self._incoming_sample_rate = expected_sample_rate
        self._incoming_channels = expected_channels
        self._incoming_format = "s16le"
        self._incoming_sequence = 0

        self._server: Serve | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_event = asyncio.Event()
        self._client_tasks: Set[asyncio.Task[None]] = set()
        self._send_queues: Set[asyncio.Queue[object]] = set()
        self._drop_counters: dict[str, int] = {}
        self._playback_sequence = 0
        self._current_tts_id: str | None = None
        self._current_effect_id: str | None = None

        self.sample_rate = expected_sample_rate
        self.frame_samples = 512
        self.log = configure_logging("audio.robot_stream")

    async def start(self) -> None:
        """Запускает WebSocket-сервер."""

        if self._server is not None:
            return
        self._loop = asyncio.get_running_loop()
        self._stop_event.clear()
        self.log.info(
            "Запускаю WebSocket-сервер аудио",
            extra={
                "attrs": {
                    "host": self._host,
                    "port": self._port,
                    "path": self._path,
                    "accepted_paths": sorted(self._accepted_paths),
                    "subprotocol": self._subprotocol or "",
                }
            },
        )
        self._server = await serve(
            self._handle_robot,
            host=self._host,
            port=self._port,
            subprotocols=[self._subprotocol] if self._subprotocol else None,
            # ВАЖНО: стандартный keepalive websockets плохо дружит с ESP32.
            # Под нагрузкой TTS микроконтроллер может не успеть ответить pong,
            # и Python сам закрывает соединение кодом 1011. Аудиопоток и так
            # постоянно идёт от робота, поэтому отдельный ping здесь не нужен.
            ping_interval=None,
            ping_timeout=None,
            max_size=None,
        )

    def stop(self) -> bool:
        """Останавливает сервер и будит ожидающих читателей."""

        if self._server is None or self._loop is None:
            return False
        self.log.info("Останавливаю WebSocket-сервер аудио")
        self._stop_event.set()
        self._loop.call_soon_threadsafe(self._shutdown)
        return True

    def _shutdown(self) -> None:
        if self._server is not None:
            self._server.close()
            asyncio.create_task(self._server.wait_closed())
            self._server = None
        for task in list(self._client_tasks):
            task.cancel()
        self._put_sentinel()

    def _put_sentinel(self) -> None:
        try:
            self._queue.put_nowait(self._SENTINEL)
        except asyncio.QueueFull:
            with contextlib.suppress(asyncio.QueueEmpty):
                _ = self._queue.get_nowait()
            self._queue.put_nowait(self._SENTINEL)

    async def read(self) -> RobotAudioFrame:
        item = await self._queue.get()
        if item is self._SENTINEL:
            raise RobotStreamClosed()
        return item

    async def _handle_robot(self, websocket: WebSocketServerProtocol) -> None:
        global _LAST_ROBOT_HOST, _LAST_ROBOT_PEER
        peer = (
            f"{websocket.remote_address[0]}:{websocket.remote_address[1]}"
            if websocket.remote_address
            else "unknown"
        )
        if websocket.remote_address:
            _LAST_ROBOT_HOST = str(websocket.remote_address[0])
            _LAST_ROBOT_PEER = peer
        if websocket.path not in self._accepted_paths:
            self.log.warning(
                "Отклоняю подключение: неверный путь",
                extra={"attrs": {"expected": sorted(self._accepted_paths), "got": websocket.path}},
            )
            await websocket.close(code=4404, reason="invalid path")
            return
        if self._authorization:
            auth = websocket.request_headers.get("Authorization", "")
            if auth != self._authorization:
                self.log.warning("Отклоняю подключение: неверный Authorization")
                await websocket.close(code=4403, reason="unauthorized")
                return

        self.log.info(
            "Робот подключился",
            extra={
                "attrs": {
                    "peer": peer,
                    "path": websocket.path,
                    "subprotocol": websocket.subprotocol or "",
                }
            },
        )
        task = asyncio.current_task()
        if task is not None:
            self._client_tasks.add(task)
        send_queue: asyncio.Queue[object] = asyncio.Queue(maxsize=self._send_queue_max)
        self._send_queues.add(send_queue)
        sender_task = asyncio.create_task(self._send_loop(websocket, send_queue, peer))
        # Не запускаем WebSocket ping-watchdog: ESP32 сам постоянно шлёт аудио,
        # а принудительные ping/pong уже приводили к обрывам длинных ответов.
        heartbeat_task: asyncio.Task[None] | None = None

        # Простое рукопожатие. Старые прошивки это просто залогируют и продолжат работу.
        self._enqueue_json(
            send_queue,
            {
                "type": "server_ready",
                "protocol": "jarvis-audio-v1",
                "audio_in": "af-v1-or-raw-pcm16",
                "audio_out": "pcm16-json-boundaries",
            },
            purpose="server_ready",
        )

        try:
            async for message in websocket:
                if isinstance(message, str):
                    self._handle_text_message(message, peer)
                else:
                    self._handle_binary_message(message)
        except asyncio.CancelledError:
            raise
        except ConnectionClosedOK:
            self.log.info("Робот корректно отключился", extra={"attrs": {"peer": peer}})
        except ConnectionClosedError as exc:
            self.log.warning(
                "Соединение закрыто с ошибкой",
                extra={"attrs": {"peer": peer, "code": exc.code, "reason": exc.reason}},
            )
        except Exception:
            self.log.exception("Ошибка при обработке аудиопотока робота")
        finally:
            if task is not None:
                self._client_tasks.discard(task)
            self._send_queues.discard(send_queue)
            sender_task.cancel()
            if heartbeat_task is not None:
                heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sender_task
            if heartbeat_task is not None:
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat_task
            self.log.info(
                "Соединение с роботом завершено",
                extra={
                    "attrs": {
                        "peer": peer,
                        "close_code": websocket.close_code,
                        "close_reason": websocket.close_reason,
                    }
                },
            )

    def _handle_text_message(self, message: str, peer: str) -> None:
        try:
            payload = json.loads(message)
        except json.JSONDecodeError:
            self.log.warning(
                "Получен невалидный JSON от робота",
                extra={"attrs": {"peer": peer, "text": message[:128]}},
            )
            return

        msg_type = payload.get("type", "")
        if msg_type in {"robot_hello", "hello"}:
            self.log.info("Рукопожатие робота", extra={"attrs": {"peer": peer, "payload": payload}})
            return
        if msg_type == "mic_chunk":
            self._incoming_sample_rate = int(payload.get("sample_rate", self._incoming_sample_rate))
            self._incoming_channels = int(payload.get("channels", self._incoming_channels))
            self._incoming_format = str(payload.get("sample_format", self._incoming_format))
            self.log.info(
                "Обновлены параметры raw PCM микрофона",
                extra={
                    "attrs": {
                        "peer": peer,
                        "sample_rate": self._incoming_sample_rate,
                        "channels": self._incoming_channels,
                        "sample_format": self._incoming_format,
                    }
                },
            )
            return
        self.log.debug("Текстовое сообщение от робота", extra={"attrs": {"peer": peer, "text": payload}})

    def _handle_binary_message(self, message: bytes) -> None:
        if not message:
            self.log.warning("Получен пустой бинарный кадр от робота")
            return
        frame = self._decode_frame(message)
        if frame is None:
            return
        if self._queue.full():
            dropped = self._queue.get_nowait()
            if isinstance(dropped, RobotAudioFrame):
                self.log.warning("Буфер STT переполнен, удаляю старый кадр", extra={"attrs": {"sequence": dropped.sequence}})
        self._queue.put_nowait(frame)

    def _decode_frame(self, payload: bytes) -> RobotAudioFrame | None:
        if len(payload) >= self._AF_HEADER_SIZE and payload[0:2] == b"AF":
            return self._decode_af_v1(payload)
        return self._decode_raw_pcm(payload)

    def _decode_af_v1(self, payload: bytes) -> RobotAudioFrame | None:
        header = self._parse_af_header(payload)
        if header is None:
            return None
        pcm_end = header.payload_offset + header.pcm_bytes
        if pcm_end > len(payload):
            self.log.warning(
                "AF кадр короче заявленного PCM",
                extra={"attrs": {"payload": len(payload), "pcm_bytes": header.pcm_bytes}},
            )
            return None
        pcm = payload[header.payload_offset:pcm_end]
        if header.sample_bits != 16:
            self.log.warning("AF кадр с неподдерживаемой битностью", extra={"attrs": {"bits": header.sample_bits}})
            return None
        if len(pcm) % 2 != 0:
            self.log.warning("AF PCM не делится на 2 байта", extra={"attrs": {"pcm_bytes": len(pcm)}})
            return None
        if header.channels > 1 and (len(pcm) // 2) % header.channels != 0:
            self.log.warning(
                "AF PCM не делится на количество каналов",
                extra={"attrs": {"pcm_bytes": len(pcm), "channels": header.channels}},
            )
            return None

        pcm_mono = select_stt_pcm(
            pcm,
            header.channels,
            mode=self._stt_channel,
            rms_left=header.rms_left,
            rms_right=header.rms_right,
        )
        frame_samples = len(pcm_mono) // 2
        self.sample_rate = header.sample_rate
        self.frame_samples = frame_samples
        self._incoming_sequence = header.sequence

        return RobotAudioFrame(
            sequence=header.sequence,
            timestamp_us=header.timestamp_us,
            sample_rate=header.sample_rate,
            frame_samples=frame_samples,
            channels=header.channels,
            sample_bits=header.sample_bits,
            pcm_stereo=pcm,
            pcm_mono=pcm_mono,
            rms_left=header.rms_left,
            rms_right=header.rms_right,
            mic_spacing_m=header.mic_spacing_m,
            direction_deg=header.direction_deg,
            confidence=header.confidence,
            localization_enabled=header.localization_enabled,
        )

    def _parse_af_header(self, payload: bytes) -> _AfHeader | None:
        if len(payload) < self._AF_HEADER_SIZE:
            return None
        version = payload[2]
        flags = payload[3]
        if version != 1:
            self.log.warning("Неподдерживаемая версия AF", extra={"attrs": {"version": version}})
            return None
        try:
            (
                sequence,
                timestamp_us,
                sample_rate,
                frame_samples,
                channels,
                sample_bits,
                pcm_bytes,
                rms_left,
                rms_right,
                mic_spacing_m,
                direction_deg,
                confidence,
            ) = struct.unpack_from("<IQIIHHIfffff", payload, 4)
        except struct.error:
            self.log.exception("Не удалось разобрать AF заголовок")
            return None
        if channels < 1 or channels > 8:
            self.log.warning("Некорректное число каналов AF", extra={"attrs": {"channels": channels}})
            return None
        return _AfHeader(
            sequence=sequence,
            timestamp_us=timestamp_us,
            sample_rate=sample_rate or self._expected_sample_rate,
            frame_samples=frame_samples,
            channels=channels,
            sample_bits=sample_bits,
            pcm_bytes=pcm_bytes,
            rms_left=rms_left,
            rms_right=rms_right,
            mic_spacing_m=mic_spacing_m,
            direction_deg=direction_deg,
            confidence=confidence,
            localization_enabled=bool(flags & 0x01),
            payload_offset=self._AF_HEADER_SIZE,
        )

    def _decode_raw_pcm(self, payload: bytes) -> RobotAudioFrame | None:
        if len(payload) % 2 != 0:
            self.log.warning("Raw PCM не делится на 2 байта", extra={"attrs": {"pcm_bytes": len(payload)}})
            return None
        if self._incoming_format.lower() != "s16le":
            self.log.warning("Неподдерживаемый raw формат", extra={"attrs": {"format": self._incoming_format}})
            return None
        channels = max(1, self._incoming_channels or self._expected_channels)
        if channels > 1 and (len(payload) // 2) % channels != 0:
            self.log.warning("Raw PCM не делится на каналы", extra={"attrs": {"channels": channels, "bytes": len(payload)}})
            return None

        samples = array("h")
        samples.frombytes(payload)
        if channels == 1:
            rms_left = rms_right = _rms_int16(samples)
            pcm_mono = payload
        else:
            rms_left = _rms_int16(samples[0::channels])
            rms_right = _rms_int16(samples[1::channels])
            pcm_mono = select_stt_pcm(
                payload,
                channels,
                mode=self._stt_channel,
                rms_left=rms_left,
                rms_right=rms_right,
            )

        self._incoming_sequence = (self._incoming_sequence + 1) & 0xFFFFFFFF
        frame_samples = len(pcm_mono) // 2
        sample_rate = self._incoming_sample_rate or self._expected_sample_rate
        self.sample_rate = sample_rate
        self.frame_samples = frame_samples
        return RobotAudioFrame(
            sequence=self._incoming_sequence,
            timestamp_us=int(time.time() * 1_000_000),
            sample_rate=sample_rate,
            frame_samples=frame_samples,
            channels=channels,
            sample_bits=16,
            pcm_stereo=payload,
            pcm_mono=pcm_mono,
            rms_left=rms_left,
            rms_right=rms_right,
            mic_spacing_m=0.0,
            direction_deg=0.0,
            confidence=0.0,
            localization_enabled=False,
        )

    async def _send_loop(self, websocket: WebSocketServerProtocol, queue: asyncio.Queue[object], peer: str) -> None:
        out_sample_rate = 16_000
        out_channels = 1
        in_audio_stream = False
        audio_bytes_sent = 0
        burst_bytes = 0
        pace_anchor_time: float | None = None
        pace_anchor_bytes = 0
        try:
            while True:
                payload = await queue.get()

                if isinstance(payload, str):
                    try:
                        msg = json.loads(payload)
                    except Exception:
                        msg = {}
                    msg_type = msg.get("type")
                    if msg_type == "audio_start":
                        out_sample_rate = max(1, int(msg.get("sample_rate") or out_sample_rate))
                        out_channels = max(1, int(msg.get("channels") or out_channels))
                        bytes_per_second = max(1, out_sample_rate * out_channels * 2)
                        burst_bytes = int(bytes_per_second * self._tts_initial_burst_ms / 1000.0)
                        audio_bytes_sent = 0
                        pace_anchor_time = None
                        pace_anchor_bytes = 0
                        in_audio_stream = True
                    elif msg_type == "audio_end":
                        in_audio_stream = False
                        pace_anchor_time = None

                await websocket.send(payload)

                # Отправляем PCM с предбуфером и без накопления ошибки таймера.
                # Раньше после burst мы делали sleep после каждого чанка. Если
                # Windows/asyncio задерживали sleep на несколько миллисекунд,
                # ошибка накапливалась, и к середине длинной фразы ESP32 могла
                # получить underrun — это слышалось как лёгкое заикание.
                # Теперь используем общий дедлайн: если один sleep проспал
                # дольше нужного, следующие чанки временно идут без паузы и
                # восстанавливают запас в очереди робота.
                if isinstance(payload, (bytes, bytearray)) and in_audio_stream:
                    bytes_per_second = max(1, out_sample_rate * out_channels * 2)
                    duration = len(payload) / float(bytes_per_second)
                    audio_bytes_sent += len(payload)
                    if duration > 0:
                        if audio_bytes_sent <= burst_bytes:
                            # Стартовый запас отправляем почти сразу, но с
                            # крошечной паузой, чтобы не забить TCP-буфер одним
                            # большим залпом на слабом Wi‑Fi.
                            await asyncio.sleep(min(duration * 0.05, 0.004))
                        else:
                            if pace_anchor_time is None:
                                pace_anchor_time = time.perf_counter()
                                pace_anchor_bytes = audio_bytes_sent
                            else:
                                audio_after_anchor = audio_bytes_sent - pace_anchor_bytes
                                target_time = pace_anchor_time + (
                                    audio_after_anchor / float(bytes_per_second)
                                ) * self._outgoing_audio_pace
                                delay = target_time - time.perf_counter()
                                if delay > 0:
                                    await asyncio.sleep(min(delay, 0.120))
        except asyncio.CancelledError:
            pass
        except (ConnectionClosedOK, ConnectionClosedError, ConnectionResetError, OSError) as exc:
            # Для ESP32/Wi‑Fi разрыв во время длинной озвучки — штатная сетевая
            # авария, а не баг приложения. Логируем коротко без огромного
            # traceback: обработчик подключения ниже зафиксирует закрытие и
            # робот сам переподключится.
            self.log.warning(
                "Отправка роботу прервана: соединение закрыто",
                extra={"attrs": {"peer": peer, "error": str(exc)[:160]}},
            )
        except Exception:
            self.log.exception("Ошибка отправки данных роботу", extra={"attrs": {"peer": peer}})

    async def _ping_watchdog(self, websocket: WebSocketServerProtocol, peer: str) -> None:
        if not self._ping_interval or self._ping_interval <= 0:
            return
        try:
            while True:
                await asyncio.sleep(self._ping_interval)
                if websocket.closed:
                    return
                try:
                    pong_waiter = websocket.ping()
                    await asyncio.wait_for(pong_waiter, timeout=self._ping_timeout or self._ping_interval)
                except asyncio.TimeoutError:
                    self.log.warning("Робот не ответил pong", extra={"attrs": {"peer": peer}})
                    await websocket.close(code=4000, reason="ping timeout")
                    return
        except asyncio.CancelledError:
            pass

    def _next_playback_sequence(self) -> int:
        self._playback_sequence = (self._playback_sequence + 1) & 0xFFFFFFFF
        return self._playback_sequence

    def _enqueue_json(self, queue: asyncio.Queue[object], payload: dict, *, purpose: str) -> None:
        self._enqueue_to_queue(queue, json.dumps(payload), purpose=purpose)

    def _broadcast_json(self, payload: dict, *, purpose: str) -> None:
        serialized = json.dumps(payload)
        self._broadcast(serialized, purpose=purpose)

    def _broadcast_binary(self, payload: bytes, *, purpose: str) -> None:
        self._broadcast(payload, purpose=purpose)

    def _broadcast(self, payload: object, *, purpose: str) -> None:
        if self._loop is None:
            self.log.warning("Event loop не готов, %s не отправлен", purpose)
            return

        def _enqueue() -> None:
            if not self._send_queues:
                self.log.debug("Нет активного робота для отправки %s", purpose)
                return
            for queue in list(self._send_queues):
                self._enqueue_to_queue(queue, payload, purpose=purpose)

        self._loop.call_soon_threadsafe(_enqueue)

    def _enqueue_to_queue(self, queue: asyncio.Queue[object], payload: object, *, purpose: str) -> None:
        if queue.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                _ = queue.get_nowait()
                self._drop_counters[purpose] = self._drop_counters.get(purpose, 0) + 1
        queue.put_nowait(payload)
        dropped = self._drop_counters.pop(purpose, 0)
        if dropped:
            self.log.warning(
                "Очередь отправки переполнена, старые элементы удалены",
                extra={"attrs": {"purpose": purpose, "dropped": dropped, "queue_max": queue.maxsize}},
            )

    def _iter_chunked(self, payload: bytes) -> Iterable[bytes]:
        if not payload:
            return []
        size = self._max_outgoing_frame
        return (payload[idx : idx + size] for idx in range(0, len(payload), size))

    def _send_audio_start(self, audio_id: str, *, kind: str, sample_rate: int, channels: int, volume: float = 1.0) -> None:
        self._broadcast_json(
            {
                "type": "audio_start",
                "id": audio_id,
                "kind": kind,
                "sample_rate": sample_rate,
                "sample_format": "s16le",
                "channels": channels,
                "volume": volume,
            },
            purpose=f"audio_start/{kind}",
        )

    def _send_audio_end(self, audio_id: str) -> None:
        self._broadcast_json({"type": "audio_end", "id": audio_id}, purpose="audio_end")

    def _send_tts_preroll(self, sample_rate: int, channels: int) -> None:
        """Отправляет короткую тишину перед речью.

        Это не меняет сам TTS, но даёт ESP32/MAX98357A небольшой предбуфер.
        На практике это убирает обрезание первых букв и снижает риск
        микропровалов на старте ответа после быстрого partial-STT.
        """

        if self._tts_preroll_ms <= 0:
            return
        rate = max(1, int(sample_rate))
        ch = max(1, int(channels))
        samples = max(1, int(rate * self._tts_preroll_ms / 1000.0))
        silence = bytes(samples * ch * 2)
        for piece in self._iter_chunked(silence):
            self._broadcast_binary(piece, purpose="tts_preroll")


    def send_tts(
        self,
        pcm: bytes,
        sample_rate: int,
        *,
        text: str,
        preset: str,
        chunk_index: int,
        chunks_total: int,
        volume: float,
        channels: int = 1,
    ) -> None:
        if chunk_index <= 1 or self._current_tts_id is None:
            if self._current_tts_id is not None:
                self._send_audio_end(self._current_tts_id)
            self._current_tts_id = f"tts-{self._next_playback_sequence()}-{int(time.time() * 1000)}"
            self._send_audio_start(self._current_tts_id, kind="tts", sample_rate=sample_rate, channels=channels, volume=volume)
            self._send_tts_preroll(sample_rate, channels)

        for piece in self._iter_chunked(pcm):
            self._broadcast_binary(piece, purpose="tts_pcm")

        if chunk_index >= chunks_total and self._current_tts_id is not None:
            self._send_audio_end(self._current_tts_id)
            self._current_tts_id = None

    def send_effect(
        self,
        pcm: bytes,
        sample_rate: int,
        *,
        name: str,
        source_file: str,
        repeat_index: int,
        repeat_total: int,
        volume: float,
        channels: int = 1,
    ) -> None:
        if repeat_index <= 1 or self._current_effect_id is None:
            if self._current_effect_id is not None:
                self._send_audio_end(self._current_effect_id)
            self._current_effect_id = f"emotion-{self._next_playback_sequence()}-{int(time.time() * 1000)}"
            self._send_audio_start(self._current_effect_id, kind="emotion", sample_rate=sample_rate, channels=channels, volume=volume)

        for piece in self._iter_chunked(pcm):
            self._broadcast_binary(piece, purpose="effect_pcm")

        if repeat_index >= repeat_total and self._current_effect_id is not None:
            self._send_audio_end(self._current_effect_id)
            self._current_effect_id = None

    def forward_tts_chunk(self, pcm: bytes, sample_rate: int, *, text: str, preset: str, chunk_index: int, chunks_total: int, volume: float) -> None:
        try:
            self.send_tts(pcm, sample_rate, text=text, preset=preset, chunk_index=chunk_index, chunks_total=chunks_total, volume=volume, channels=1)
        except Exception:
            self.log.exception("Не удалось передать TTS роботу")

    def forward_effect_chunk(self, pcm: bytes, sample_rate: int, *, name: str, source_file: str, repeat_index: int, repeat_total: int, volume: float, channels: int = 1) -> None:
        try:
            self.send_effect(pcm, sample_rate, name=name, source_file=source_file, repeat_index=repeat_index, repeat_total=repeat_total, volume=volume, channels=channels)
        except Exception:
            self.log.exception("Не удалось передать эффект роботу", extra={"attrs": {"effect": name, "file": source_file}})


def _rms_int16(values: Iterable[int]) -> float:
    vals = list(values)
    if not vals:
        return 0.0
    return math.sqrt(sum(v * v for v in vals) / len(vals)) / 32768.0


def select_stt_pcm(
    pcm: bytes,
    channels: int,
    *,
    mode: str = "best",
    rms_left: float = 0.0,
    rms_right: float = 0.0,
) -> bytes:
    """Возвращает моно PCM16 для Vosk.

    Для распознавания речи не всегда лучше усреднять два микрофона: если один
    канал тише, шумнее или даёт фазовые провалы, среднее может ухудшить wake-word.
    Режим ``best`` берёт более сильный канал только при явном перевесе, иначе
    оставляет аккуратный микс.
    """

    if channels <= 1:
        return pcm

    mode = (mode or "best").lower()
    if mode == "mix":
        return downmix_to_mono(pcm, channels)

    if mode == "left":
        return extract_channel_pcm16(pcm, channels, 0)

    if mode == "right":
        return extract_channel_pcm16(pcm, channels, min(1, channels - 1))

    # best: не прыгаем между каналами от малой разницы. Берём отдельный канал
    # только если он заметно сильнее; иначе миксуем оба.
    left = max(float(rms_left), 1e-6)
    right = max(float(rms_right), 1e-6)
    if left > right * 1.35:
        return extract_channel_pcm16(pcm, channels, 0)
    if right > left * 1.35:
        return extract_channel_pcm16(pcm, channels, min(1, channels - 1))
    return downmix_to_mono(pcm, channels)


def extract_channel_pcm16(pcm: bytes, channels: int, channel_index: int) -> bytes:
    """Достаёт один канал из interleaved PCM16."""

    if channels <= 1:
        return pcm
    channel_index = max(0, min(channel_index, channels - 1))
    samples = struct.unpack_from("<" + "h" * (len(pcm) // 2), pcm)
    selected = samples[channel_index::channels]
    return struct.pack("<" + "h" * len(selected), *selected)


def downmix_to_mono(pcm: bytes, channels: int) -> bytes:
    """Сводит интерливированный PCM16 в моно."""

    if channels <= 1:
        return pcm
    samples = struct.unpack_from("<" + "h" * (len(pcm) // 2), pcm)
    mono: Deque[int] = deque(maxlen=len(samples) // channels)
    for idx in range(0, len(samples), channels):
        chunk = samples[idx : idx + channels]
        mono.append(int(sum(chunk) / len(chunk)))
    return struct.pack("<" + "h" * len(mono), *mono)
