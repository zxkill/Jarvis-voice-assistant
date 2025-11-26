"""Приём и отправка аудио роботу по WebSocket."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import math
import logging
import struct
import time
from array import array
from collections import deque
from typing import Deque, Dict, Set
from urllib.parse import urlparse

import websockets
from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK
from websockets.legacy.server import Serve, WebSocketServerProtocol, serve

from core.logging_json import configure_logging

_HEADER_STRUCT = struct.Struct("<2sBBIQIIHHIfffff")
# Заголовок исходящих кадров TTS, описанный в прошивке ESP32.
_PLAYBACK_HEADER_STRUCT = struct.Struct("<2sBBIIIHHIIff")


@dataclasses.dataclass(slots=True)
class PlaybackClientCaps:
    """Описание возможностей конкретного подключённого клиента.

    Поля завязаны на протокол XiaoZhi, так как теперь сервер может работать в
    двух режимах: наш старый ``AF`` и совместимый ``BinaryProtocol2/3``.
    Храним параметры, полученные из hello-сообщения, чтобы формировать
    корректные ответы (sample_rate, channels, frame_duration) и понимать,
    как декодировать входящие бинарные кадры.

    По умолчанию используем профиль XiaoZhi (PCM16 LE, 16 кГц, 1 канал,
    длительность кадра 60 мс). Это гарантирует, что даже до получения hello
    от робота мы будем отправлять совместимый поток, а не устаревший AF
    с 36-байтовым заголовком и слишком короткими кадрами, которые приводят
    к треску на MAX98357A.
    """

    mode: str = "xiaozhi"  # ``af`` либо ``xiaozhi``
    xiaozhi_version: int = 3
    sample_rate: int = 16_000
    channels: int = 1
    frame_duration_ms: int = 60
    format: str = "pcm16"


@dataclasses.dataclass(slots=True)
class PlaybackQueueItem:
    """Элемент очереди отправки аудио роботу.

    Хранит полезную нагрузку, её назначение (TTS/эффект) и время постановки
    в очередь, чтобы можно было измерять задержку доставки и понимать, где
    возникает бутылочное горлышко.
    """

    payload: bytes
    purpose: str
    enqueued_at: float


@dataclasses.dataclass(slots=True)
class PlaybackStats:
    """Собирает статистику отправки для одного WebSocket-подключения."""

    connected_at: float
    sent_frames: int = 0
    sent_bytes: int = 0
    dropped_frames: int = 0
    dropped_bytes: int = 0
    max_queue_depth: int = 0
    max_latency_ms: float = 0.0
    last_payload_bytes: int = 0


@dataclasses.dataclass(slots=True)
class RobotClientSession:
    """Сессионное состояние одного клиента робота.

    Каждое подключение хранит свою очередь отправки, статистику, параметры
    hello-обмена XiaoZhi и читаемое имя пира для логов.
    """

    queue: asyncio.Queue[PlaybackQueueItem]
    stats: PlaybackStats
    caps: PlaybackClientCaps
    peer: str


@dataclasses.dataclass(slots=True)
class RobotAudioFrame:
    """Структура аудиокадра, который прислал робот."""

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
    """Сигнализирует о том, что поток остановлен или завершён."""


class RobotAudioStream:
    """WebSocket-сервер, принимающий аудио от ESP32."""

    _SENTINEL = object()

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
        max_playback_payload: int = 2048,
        playback_queue_max: int = 200,
    ) -> None:
        """Создаёт сервер, принимающий бинарные кадры PCM16 от робота."""

        self._parsed = urlparse(endpoint)
        if self._parsed.scheme not in {"ws", "wss"}:
            raise ValueError("endpoint должен начинаться с ws:// или wss://")
        self._host = self._parsed.hostname or "0.0.0.0"
        # Порт ``0`` используется тестами и означает «выбрать свободный автоматически»,
        # поэтому обрабатываем ``None`` отдельно, не полагаясь на truthy-логику.
        self._port = self._parsed.port if self._parsed.port is not None else 8765
        self._path = self._parsed.path or "/"
        self._subprotocol = subprotocol
        self._authorization = authorization
        self._ping_interval = ping_interval
        self._ping_timeout = ping_timeout
        self._queue: asyncio.Queue[RobotAudioFrame | object] = asyncio.Queue(
            maxsize=max(1, queue_max)
        )
        self._expected_sample_rate = expected_sample_rate
        self._expected_channels = expected_channels
        self._server: Serve | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_event = asyncio.Event()
        self._client_tasks: Set[asyncio.Task[None]] = set()
        # Список активных сессий клиентов: каждая хранит очередь исходящих
        # кадров, статистику и параметры hello XiaoZhi.
        self._sessions: list[RobotClientSession] = []
        self.sample_rate = expected_sample_rate
        self.frame_samples = 512
        self.log = configure_logging("audio.robot_stream")
        # Счётчик исходящих кадров, общий для TTS и фоновых эффектов.
        self._playback_sequence = 0
        # Жёсткий лимит полезной нагрузки одного кадра, чтобы не получить ошибку
        # 1009 (frame too large) от прошивки ESP32 или прокси на пути. Значение
        # чуть меньше килобайта по умолчанию, потому что часть байт забирает
        # WebSocket‑фрейм, и реальные ограничения прошивки могут отличаться.
        # Ограничиваем размер полезной нагрузки исходящего кадра. Значение
        # подобрано под 60 мс PCM16/16 кГц (около 1920 байт) с запасом на
        # заголовок, чтобы кадры XiaoZhi не резались и не давали треск. Всё,
        # что меньше 512, повышаем до безопасного минимума.
        self._max_playback_payload = max(512, max_playback_payload)
        # Максимальный размер очереди исходящих кадров на клиента: увеличен по
        # сравнению с прошлой версией, чтобы помещался целый пакет TTS даже при
        # мелкой нарезке на субкадры. Используем жёсткое ограничение и
        # перераспределяем старые кадры, если поток закрывается.
        self._playback_queue_max = max(10, playback_queue_max)
        # Таймстамп последнего предупреждения об отсутствии подключений, чтобы
        # не засорять логи сотнями одинаковых записей за одно событие TTS.
        self._last_no_client_warning = 0.0
        # Таймстамп последнего предупреждения о принудительном дропе кадра,
        # чтобы не засорять логи при bursts.
        self._last_drop_warning = 0.0
        # Локальный счётчик входящих кадров, когда клиент присылает XiaoZhi
        # без явного sequence: помогает проставлять понятные номера в ack и
        # логах сервера.
        self._rx_sequence = 0
        # Отдельный логгер для детализированной отладки ресемплинга, чтобы не
        # смешивать сообщения с основным потоком логов и при необходимости
        # повысить уровень именно для преобразований формата.
        self._resample_log = logging.getLogger("audio.robot_stream.resample")

    async def start(self) -> None:
        """Запускает WebSocket-сервер и ожидает подключений робота."""

        if self._server is not None:
            self.log.debug("Сервер уже запущен, повторный старт не требуется")
            return
        self._loop = asyncio.get_running_loop()
        self._stop_event.clear()
        self.log.info(
            "Запускаю WebSocket-сервер для аудио",
            extra={
                "attrs": {
                    "host": self._host,
                    "port": self._port,
                    "path": self._path,
                    "subprotocol": self._subprotocol or "",
                    "max_playback_payload": self._max_playback_payload,
                    "playback_queue_max": self._playback_queue_max,
                    "expected_sample_rate": self._expected_sample_rate,
                }
            },
        )
        self._server = await serve(
            self._handle_robot,
            host=self._host,
            port=self._port,
            subprotocols=[self._subprotocol] if self._subprotocol else None,
            ping_interval=self._ping_interval,
            ping_timeout=self._ping_timeout,
            max_size=None,
        )

    def stop(self) -> bool:
        """Останавливает сервер и уведомляет читателей очереди."""

        if self._server is None:
            self.log.debug("Сервер не запущен, остановка пропускается")
            return False
        if self._loop is None:
            self.log.debug("Event loop ещё не инициализирован, остановка пропускается")
            return False
        self.log.info("Останавливаю WebSocket-сервер аудио")
        self._stop_event.set()
        self._loop.call_soon_threadsafe(self._shutdown)
        return True

    def _shutdown(self) -> None:
        """Закрывает сервер и активные подключения."""

        if self._server is not None:
            self._server.close()
            asyncio.create_task(self._server.wait_closed())
            self._server = None
        for task in list(self._client_tasks):
            task.cancel()
        try:
            self._queue.put_nowait(self._SENTINEL)
        except asyncio.QueueFull:
            _ = self._queue.get_nowait()
            self._queue.put_nowait(self._SENTINEL)

    async def read(self) -> RobotAudioFrame:
        """Возвращает следующий кадр из очереди или выбрасывает ошибку при остановке."""

        item = await self._queue.get()
        if item is self._SENTINEL:
            self.log.debug("Поток остановлен, читающим возвращается исключение")
            raise RobotStreamClosed()
        return item

    async def _handle_robot(self, websocket: WebSocketServerProtocol) -> None:
        """Принимает одно подключение робота и читает аудиокадры."""

        peer = f"{websocket.remote_address[0]}:{websocket.remote_address[1]}"
        if websocket.path != self._path:
            self.log.warning(
                "Отклоняю подключение: неверный путь",
                extra={"attrs": {"expected": self._path, "got": websocket.path}},
            )
            await websocket.close(code=4404, reason="invalid path")
            return
        if self._authorization:
            auth = websocket.request_headers.get("Authorization", "")
            if auth != self._authorization:
                self.log.warning("Отклоняю подключение: неверный Authorization")
                await websocket.close(code=4403, reason="unauthorized")
                return
        self.log.info("Робот подключился", extra={"attrs": {"peer": peer}})
        task = asyncio.current_task()
        if task is not None:
            self._client_tasks.add(task)
        # Очередь исходящих кадров увеличена и задаётся конфигом: это позволяет
        # безопасно складывать все субкадры TTS даже при жёстком лимите полезной
        # нагрузки (например, 600–800 байт на кадр для MAX98357A).
        send_queue: asyncio.Queue[PlaybackQueueItem] = asyncio.Queue(
            maxsize=self._playback_queue_max
        )
        stats = PlaybackStats(connected_at=time.time())
        # Инициализируем capabilities сразу в формате XiaoZhi с частотой,
        # заданной конфигом. Это позволяет стримить роботу корректные 60 мс
        # кадры даже если hello по каким-то причинам не пришёл (например,
        # временная потеря пакета при старте).
        session = RobotClientSession(
            queue=send_queue,
            stats=stats,
            caps=PlaybackClientCaps(
                sample_rate=self._expected_sample_rate,
                channels=1,
                frame_duration_ms=60,
            ),
            peer=peer,
        )
        sender_task = asyncio.create_task(
            self._send_loop(websocket, session)
        )
        self._sessions.append(session)
        self.log.debug(
            "Инициализированы параметры XiaoZhi по умолчанию",
            extra={
                "attrs": {
                    "peer": peer,
                    "mode": session.caps.mode,
                    "sample_rate": session.caps.sample_rate,
                    "channels": session.caps.channels,
                    "frame_duration_ms": session.caps.frame_duration_ms,
                    "max_payload": self._max_playback_payload,
                }
            },
        )
        try:
            async for message in websocket:
                if isinstance(message, str):
                    self._handle_text_message(message, websocket, session)
                    continue
                frame = self._decode_incoming_frame(message, session)
                if frame is None:
                    continue
                if self._queue.full():
                    dropped = self._queue.get_nowait()
                    if isinstance(dropped, RobotAudioFrame):
                        self.log.warning(
                            "Буфер переполнен, удаляю кадр",
                            extra={"attrs": {"sequence": dropped.sequence}},
                        )
                self._queue.put_nowait(frame)
                self.log.debug(
                    "Кадр добавлен в очередь",
                    extra={
                        "attrs": {
                            "sequence": frame.sequence,
                            "timestamp_us": frame.timestamp_us,
                            "queue_size": self._queue.qsize(),
                        }
                    },
                )
                # Отправляем подтверждение, чтобы робот знал, что кадр успешно получен.
                ack = json.dumps({"type": "ack", "sequence": frame.sequence})
                await websocket.send(ack)
        except asyncio.CancelledError:  # pragma: no cover - закрытие при остановке
            self.log.debug("Обработка робота отменена")
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
            sender_task.cancel()
            if session in self._sessions:
                self._sessions.remove(session)
            self.log.info("Соединение с роботом завершено", extra={"attrs": {"peer": peer}})

    async def _send_loop(
        self,
        websocket: WebSocketServerProtocol,
        session: RobotClientSession,
    ) -> None:
        """Отправляет накопленные чанки озвучки на робота."""

        self.log.debug(
            "Запущен цикл отправки TTS",
            extra={"attrs": {"peer": session.peer, "mode": session.caps.mode}},
        )
        try:
            while True:
                item = await session.queue.get()
                send_started = time.monotonic()
                await websocket.send(item.payload)
                try:
                    (
                        _magic,
                        _ver,
                        _flags,
                        seq,
                        _ts,
                        _sr,
                        _ch,
                        _bits,
                        _frame_samples,
                        pcm_bytes,
                        _volume,
                        _reserved,
                    ) = _PLAYBACK_HEADER_STRUCT.unpack_from(item.payload)
                except struct.error:
                    seq = -1
                    pcm_bytes = len(item.payload)

                queue_depth = session.queue.qsize()
                stats = session.stats
                stats.sent_frames += 1
                stats.sent_bytes += pcm_bytes
                stats.max_queue_depth = max(stats.max_queue_depth, queue_depth)
                latency_ms = (send_started - item.enqueued_at) * 1000.0
                stats.max_latency_ms = max(stats.max_latency_ms, latency_ms)
                stats.last_payload_bytes = pcm_bytes
                self.log.debug(
                    "Отправлен аудиокадр роботу",
                    extra={
                        "attrs": {
                            "peer": session.peer,
                            "size": len(item.payload),
                            "sequence": seq,
                            "pcm_bytes": pcm_bytes,
                            "queue_depth": queue_depth,
                            "latency_ms": round(latency_ms, 2),
                            "purpose": item.purpose,
                            "caps": session.caps.mode,
                        }
                    },
                )
        except asyncio.CancelledError:
            self.log.debug(
                "Цикл отправки TTS остановлен",
                extra={"attrs": {"peer": session.peer, "mode": session.caps.mode}},
            )
        except ConnectionClosedError as exc:
            # Соединение могло быть закрыто роботом при перезагрузке или потере Wi‑Fi,
            # поэтому возвращаем понятный лог и завершаем цикл без пробрасывания
            # исключения в event loop.
            self.log.warning(
                "Отправка аудио прекращена: WebSocket закрыт",
                extra={
                    "attrs": {
                        "peer": session.peer,
                        "code": exc.code,
                        "reason": exc.reason,
                    }
                },
            )
            if exc.code == 1009:
                self.log.warning(
                    "Робот закрыл канал из-за размера кадра; уменьшите max_playback_payload",
                    extra={
                        "attrs": {
                            "peer": session.peer,
                            "suggested": max(256, self._max_playback_payload // 2),
                        }
                    },
                )
        except ConnectionClosedOK as exc:
            # Робот сам закрыл соединение штатно — фиксируем событие для мониторинга.
            self.log.info(
                "Робот штатно закрыл аудиоканал",
                extra={
                    "attrs": {
                        "peer": session.peer,
                        "code": exc.code,
                        "reason": exc.reason,
                    }
                },
            )
        except Exception:
            self.log.exception(
                "Ошибка отправки аудио роботу",
                extra={"attrs": {"peer": session.peer, "mode": session.caps.mode}},
            )
        finally:
            # Дополнительно сигнализируем о завершении цикла, чтобы понимать
            # причину остановки в длинных логах.
            self.log.debug(
                "Цикл отправки TTS завершён",
                extra={
                    "attrs": {
                        "peer": session.peer,
                        "mode": session.caps.mode,
                        "sent_frames": session.stats.sent_frames,
                        "sent_bytes": session.stats.sent_bytes,
                        "dropped_frames": session.stats.dropped_frames,
                        "dropped_bytes": session.stats.dropped_bytes,
                        "max_queue_depth": session.stats.max_queue_depth,
                        "max_latency_ms": round(session.stats.max_latency_ms, 2),
                        "last_payload_bytes": session.stats.last_payload_bytes,
                        "connected_sec": round(time.time() - session.stats.connected_at, 2),
                    }
                },
            )

    def _next_playback_sequence(self) -> int:
        """Возвращает следующий номер кадра исходящего аудио."""

        self._playback_sequence = (self._playback_sequence + 1) & 0xFFFFFFFF
        return self._playback_sequence

    def _next_rx_sequence(self) -> int:
        """Простая монотонная нумерация входящих кадров XiaoZhi для ack."""

        self._rx_sequence = (self._rx_sequence + 1) & 0xFFFFFFFF
        return self._rx_sequence

    def _handle_text_message(
        self,
        message: str,
        websocket: WebSocketServerProtocol,
        session: RobotClientSession,
    ) -> None:
        """Обрабатывает текстовые сообщения (hello XiaoZhi, отладка)."""

        try:
            data = json.loads(message)
        except json.JSONDecodeError:
            self.log.warning(
                "Невалидный JSON от робота",
                extra={"attrs": {"peer": session.peer, "text": message[:200]}},
            )
            return

        msg_type = data.get("type")
        if msg_type != "hello":
            self.log.debug(
                "Текстовое сообщение от робота",
                extra={"attrs": {"peer": session.peer, "text": message}},
            )
            return

        audio_params = data.get("audio_params", {})
        session.caps.mode = "xiaozhi"
        session.caps.xiaozhi_version = int(data.get("version", 3) or 3)
        session.caps.format = audio_params.get("format", "pcm16")
        session.caps.sample_rate = int(audio_params.get("sample_rate", self._expected_sample_rate))
        session.caps.channels = int(audio_params.get("channels", 1))
        session.caps.frame_duration_ms = int(audio_params.get("frame_duration", 60))

        self.log.info(
            "Получен hello XiaoZhi от робота",
            extra={
                "attrs": {
                    "peer": session.peer,
                    "version": session.caps.xiaozhi_version,
                    "format": session.caps.format,
                    "sample_rate": session.caps.sample_rate,
                    "channels": session.caps.channels,
                    "frame_duration_ms": session.caps.frame_duration_ms,
                }
            },
        )

        server_hello = json.dumps(
            {
                "type": "hello",
                "version": session.caps.xiaozhi_version,
                "transport": "websocket",
                "audio_params": {
                    "format": "pcm16",
                    "sample_rate": self._expected_sample_rate,
                    "channels": 1,
                    "frame_duration": session.caps.frame_duration_ms,
                },
            }
        )
        asyncio.create_task(websocket.send(server_hello))

    def _decode_incoming_frame(
        self, payload: bytes, session: RobotClientSession
    ) -> RobotAudioFrame | None:
        """Определяет тип кадра и разбирает его согласно настройкам сессии."""

        if session.caps.mode == "xiaozhi":
            frame = self._decode_xiaozhi_frame(payload, session.caps)
            if frame is not None:
                return frame

        if len(payload) < _HEADER_STRUCT.size:
            self.log.warning(
                "Получен слишком короткий пакет",
                extra={"attrs": {"length": len(payload), "peer": session.peer}},
            )
            return None
        return self._decode_frame(payload)

    def _decode_xiaozhi_frame(
        self, payload: bytes, caps: PlaybackClientCaps
    ) -> RobotAudioFrame | None:
        """Разбирает BinaryProtocol2/3 из XiaoZhi и приводит к RobotAudioFrame."""

        if caps.xiaozhi_version == 2:
            if len(payload) < 16:
                self.log.warning(
                    "Короткий кадр XiaoZhi v2",
                    extra={"attrs": {"size": len(payload)}},
                )
                return None
            version = _be_u16(payload[:2])
            msg_type = _be_u16(payload[2:4])
            if version != 2 or msg_type != 0:
                return None
            size = _be_u32(payload[12:16])
            if size + 16 > len(payload):
                self.log.warning(
                    "Неверный размер кадра XiaoZhi v2",
                    extra={"attrs": {"declared": size, "actual": len(payload)}},
                )
                return None
            ts_ms = _be_u32(payload[8:12])
            pcm_payload = payload[16 : 16 + size]
        else:
            if len(payload) < 4:
                return None
            msg_type = payload[0]
            if msg_type != 0:
                return None
            size = _be_u16(payload[2:4])
            if size + 4 > len(payload):
                return None
            pcm_payload = payload[4 : 4 + size]
            ts_ms = int(time.time() * 1000)

        if len(pcm_payload) % 2 != 0:
            self.log.warning(
                "Нечётный размер PCM XiaoZhi", extra={"attrs": {"size": len(pcm_payload)}}
            )
            return None

        channels = max(1, caps.channels)
        frame_samples = len(pcm_payload) // (2 * channels)
        pcm_mono = downmix_to_mono(pcm_payload, channels)
        sequence = self._next_rx_sequence()

        self.log.debug(
            "Получен аудиокадр XiaoZhi",
            extra={
                "attrs": {
                    "sequence": sequence,
                    "ts_ms": ts_ms,
                    "channels": channels,
                    "bytes": len(pcm_payload),
                    "frame_samples": frame_samples,
                }
            },
        )

        return RobotAudioFrame(
            sequence=sequence,
            timestamp_us=ts_ms * 1000,
            sample_rate=caps.sample_rate,
            frame_samples=frame_samples,
            channels=channels,
            sample_bits=16,
            pcm_stereo=pcm_payload,
            pcm_mono=pcm_mono,
            rms_left=0.0,
            rms_right=0.0,
            mic_spacing_m=0.0,
            direction_deg=0.0,
            confidence=0.0,
            localization_enabled=False,
        )

    def _prepare_playback_payload(
        self,
        pcm: bytes,
        sample_rate: int,
        *,
        channels: int,
        volume: float,
        caps: PlaybackClientCaps,
    ) -> tuple[bytes, dict] | None:
        """Готовит бинарный пакет под конкретного клиента и возвращает метрики."""

        if not pcm:
            self.log.warning("Попытка отправить пустой PCM-чанк на робота")
            return None

        if sample_rate <= 0:
            self.log.error(
                "Некорректная частота дискретизации аудио",
                extra={"attrs": {"sample_rate": sample_rate}},
            )
            return None

        if channels <= 0:
            self.log.error(
                "Некорректное число каналов при отправке аудио",
                extra={"attrs": {"channels": channels}},
            )
            return None

        if len(pcm) % (2 * channels) != 0:
            self.log.error(
                "Размер PCM не делится на количество каналов",
                extra={"attrs": {"pcm_bytes": len(pcm), "channels": channels}},
            )
            return None

        if caps.mode == "xiaozhi":
            if caps.channels == 1 and channels != 1:
                self.log.debug(
                    "Конвертирую аудио в моно под XiaoZhi",
                    extra={"attrs": {"input_channels": channels, "peer_channels": caps.channels}},
                )
                pcm = downmix_to_mono(pcm, channels)
                channels = 1
        elif channels != 1:
            self.log.debug(
                "Конвертирую аудио в моно перед отправкой",
                extra={"attrs": {"input_channels": channels}},
            )
            pcm = downmix_to_mono(pcm, channels)
            channels = 1

        # Принудительно подгоняем частоту дискретизации под параметры робота
        # из hello. Это устраняет ситуацию, когда TTS или эффект пришёл в
        # 44.1 кГц, а XiaoZhi/ESP32 ждёт ровно 16 кГц PCM16 LE (256 кбит/с).
        if caps.sample_rate and sample_rate != caps.sample_rate:
            self._resample_log.info(
                "Привожу частоту аудио к формату робота",
                extra={
                    "attrs": {
                        "from_rate": sample_rate,
                        "to_rate": caps.sample_rate,
                        "channels": channels,
                        "mode": caps.mode,
                    }
                },
            )
            pcm = _resample_pcm(
                pcm,
                sample_rate,
                caps.sample_rate,
                max(1, channels),
                log=self._resample_log,
            )
            sample_rate = caps.sample_rate

        samples = array("h")
        samples.frombytes(pcm)
        total_samples = len(samples)
        per_channel = total_samples // channels if channels else 0
        duration_ms = (
            (per_channel / sample_rate) * 1000.0 if sample_rate > 0 and per_channel else 0.0
        )

        # Проверяем, что данные уже вписываются в лимит. На практике сюда должны
        # попадать кадры после нарезки `_split_pcm_for_caps`, поэтому превышение
        # означает ошибку конфигурации, а не штатную ситуацию: в таком случае
        # логируем и прекращаем отправку, чтобы не вносить дополнительные искажения.
        header_size = 4 if caps.mode == "xiaozhi" else _PLAYBACK_HEADER_STRUCT.size
        max_pcm_bytes = max(0, self._max_playback_payload - header_size)
        if len(pcm) > max_pcm_bytes and max_pcm_bytes > 0:
            self.log.error(
                "PCM превышает лимит полезной нагрузки — кадр отклонён",
                extra={
                    "attrs": {
                        "peer": getattr(caps, "mode", "af"),
                        "pcm_bytes": len(pcm),
                        "max_payload": self._max_playback_payload,
                        "header_bytes": header_size,
                        "channels": channels,
                    }
                },
            )
            return None

        squares_sum = sum(val * val for val in samples)
        rms_mono = (
            math.sqrt(squares_sum / total_samples) / 32768.0 if total_samples else 0.0
        )
        peak = max((abs(val) for val in samples), default=0) / 32768.0

        if caps.mode == "xiaozhi":
            timestamp_ms = int(time.time() * 1000)
            payload = _build_xiaozhi_audio_frame(caps, pcm, timestamp_ms)
            return payload, {
                "sequence": timestamp_ms,
                "duration_ms": round(duration_ms, 2),
                "peak": round(peak, 3),
                "rms": round(rms_mono, 3),
                "pcm_bytes": len(pcm),
                "sample_rate": sample_rate,
            }

        sequence = self._next_playback_sequence()
        timestamp_us = int(time.time() * 1_000_000) & 0xFFFFFFFF

        header = _PLAYBACK_HEADER_STRUCT.pack(
            b"AP",
            1,
            0,
            sequence,
            timestamp_us,
            sample_rate,
            channels,
            16,
            per_channel,
            len(pcm),
            float(volume),
            0.0,
        )

        return header + pcm, {
            "sequence": sequence,
            "duration_ms": round(duration_ms, 2),
            "peak": round(peak, 3),
            "rms": round(rms_mono, 3),
            "pcm_bytes": len(pcm),
            "sample_rate": sample_rate,
        }

    def _broadcast_payload(self, builder, *, purpose: str) -> None:
        """Отправляет подготовленный пакет во все очереди клиентов.

        ``builder`` — функция, получающая ``PlaybackClientCaps`` и возвращающая
        готовый байтовый буфер либо ``None``, если отправка в конкретную сессию
        невозможна (например, не совпадают параметры аудиоформата).
        """

        if self._loop is None:
            self.log.warning("Event loop сервера ещё не готов, %s не отправлен", purpose)
            return

        def _enqueue() -> None:
            # Если на момент отправки нет подключений, логируем предупреждение
            # не чаще раза в секунду и выходим, чтобы не засорять консоль при
            # длинных очередях TTS.
            if not self._sessions:
                now = time.monotonic()
                if now - self._last_no_client_warning > 1.0:
                    self._last_no_client_warning = now
                    self.log.warning(
                        "Нет активных подключений робота для отправки %s",
                        purpose,
                    )
                return

            for session in list(self._sessions):
                payload = builder(session.caps)
                if not payload:
                    self.log.debug(
                        "Пропускаю отправку: нет полезной нагрузки для сессии",
                        extra={"attrs": {"peer": session.peer, "purpose": purpose}},
                    )
                    continue
                queue = session.queue
                stats = session.stats
                if queue.full():
                    # Когда кадры приходят быстрее, чем робот их подтверждает,
                    # аккуратно освобождаем место и логируем агрегированно,
                    # чтобы не спамить предупреждениями на каждый субкадр.
                    dropped_frames = 0
                    dropped_bytes = 0
                    try:
                        while queue.full():
                            dropped = queue.get_nowait()
                            dropped_frames += 1
                            dropped_bytes += len(dropped.payload)
                    except asyncio.QueueEmpty:  # pragma: no cover - редкий гонк
                        pass

                    now = time.monotonic()
                    if now - self._last_drop_warning > 0.5:
                        self._last_drop_warning = now
                        self.log.warning(
                            "Очередь отправки переполнена, удаляю старые кадры",
                            extra={
                                "attrs": {
                                    "frames": dropped_frames,
                                    "bytes": dropped_bytes,
                                    "purpose": purpose,
                                    "queue_max": self._playback_queue_max,
                                    "peer": session.peer,
                                }
                            },
                        )
                    stats.dropped_frames += dropped_frames
                    stats.dropped_bytes += dropped_bytes
                queue.put_nowait(
                    PlaybackQueueItem(
                        payload=payload,
                        purpose=purpose,
                        enqueued_at=time.monotonic(),
                    )
                )
                stats.max_queue_depth = max(stats.max_queue_depth, queue.qsize())

        self._loop.call_soon_threadsafe(_enqueue)

    def _split_pcm_for_caps(
        self,
        pcm: bytes,
        *,
        channels: int,
        caps: PlaybackClientCaps,
        frame_samples_hint: int | None = None,
    ) -> list[bytes]:
        """Разбивает PCM на части, подходящие под ограничения клиента.

        В XiaoZhi клиент ожидает длительность кадра, совпадающую с ``frame_duration``
        из hello. Для AF-режима опираемся на ``frame_samples_hint``/``self.frame_samples``.
        После расчёта желаемой длины дополнительно сжимаем её до лимита WebSocket
        (``_max_playback_payload``), чтобы не ловить 1009, и выравниваем по размеру
        сэмпла конкретного числа каналов. Так мы гарантируем, что каждый кадр
        корректно декодируется на стороне ESP32 и не трещит из-за усечённых
        сэмплов.
        """

        # Размер заголовка зависит от протокола: XiaoZhi (4 байта) или AF (36 байт).
        header_size = 4 if caps.mode == "xiaozhi" else _PLAYBACK_HEADER_STRUCT.size
        # Максимальный объём полезных данных в одном WebSocket-фрейме.
        max_pcm_bytes = max(2 * channels, self._max_playback_payload - header_size)

        # Желаемая длительность кадра: для XiaoZhi используем frame_duration_ms
        # из hello, для AF — hint либо текущее значение frame_samples.
        if caps.mode == "xiaozhi":
            samples_per_frame = int(
                (caps.sample_rate * caps.frame_duration_ms) / 1000
            ) or frame_samples_hint or self.frame_samples or 512
        else:
            samples_per_frame = frame_samples_hint or self.frame_samples or 512

        bytes_per_sample = 2 * channels
        desired_bytes = max(bytes_per_sample, samples_per_frame * bytes_per_sample)
        # Упираемся в лимит полезной нагрузки и выравниваем по размеру сэмпла.
        if desired_bytes > max_pcm_bytes:
            self.log.debug(
                "Нарезаю кадр XiaoZhi под лимит WebSocket",
                extra={
                    "attrs": {
                        "desired_bytes": desired_bytes,
                        "max_pcm_bytes": max_pcm_bytes,
                        "channels": channels,
                        "frame_duration_ms": caps.frame_duration_ms,
                        "sample_rate": caps.sample_rate,
                    }
                },
            )
        frame_bytes = min(desired_bytes, max_pcm_bytes)
        frame_bytes -= frame_bytes % bytes_per_sample
        if frame_bytes <= 0:
            frame_bytes = bytes_per_sample

        return [
            pcm[i : i + frame_bytes]
            for i in range(0, len(pcm), frame_bytes)
            if pcm[i : i + frame_bytes]
        ]

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
        frame_samples: int | None = None,
    ) -> None:
        """Отправляет подготовленный PCM на робота через WebSocket."""

        if self._loop is None:
            self.log.warning("Event loop сервера ещё не готов, TTS не отправлен")
            return

        # Если нет подключённого робота, сразу фиксируем предупреждение и
        # выходим, чтобы не раздувать логи одинаковыми сообщениями на каждый
        # субкадр. При появлении клиента последующие вызовы отправят звук.
        if not self._sessions:
            now = time.monotonic()
            if now - self._last_no_client_warning > 1.0:
                self._last_no_client_warning = now
                self.log.warning("Нет активных подключений робота для отправки TTS")
            return

        self.log.debug(
            "Начало подготовки TTS",
            extra={
                "attrs": {
                    "text": text,
                    "preset": preset,
                    "chunk_index": chunk_index,
                    "chunks_total": chunks_total,
                    "clients": len(self._sessions),
                    "queue_max": self._playback_queue_max,
                }
            },
        )

        target_frame_samples = frame_samples or self.frame_samples or 512
        if target_frame_samples <= 0:
            target_frame_samples = 512

        example_caps = next(iter(self._sessions)).caps if self._sessions else PlaybackClientCaps()

        normalized_pcm, normalized_rate, normalized_channels = _normalize_audio_for_caps(
            pcm,
            sample_rate,
            channels,
            example_caps,
            resample_log=self._resample_log,
        )

        frames = self._split_pcm_for_caps(
            normalized_pcm,
            channels=normalized_channels,
            caps=example_caps,
            frame_samples_hint=target_frame_samples,
        )

        wire_header = 4 if example_caps.mode == "xiaozhi" else _PLAYBACK_HEADER_STRUCT.size
        example_frame = len(frames[0]) if frames else 0
        self.log.info(
            "Подготовка TTS к отправке",
            extra={
                "attrs": {
                    "frames": len(frames),
                    "target_frame_samples": target_frame_samples,
                    "frame_bytes": example_frame,
                    "header_bytes": wire_header,
                    "max_payload_bytes": self._max_playback_payload,
                    "chunk_index": chunk_index,
                    "chunks_total": chunks_total,
                    "pcm_bytes_total": len(normalized_pcm),
                    "volume": round(volume, 3),
                    "expected_wire_bytes": example_frame + wire_header,
                    "source_sample_rate": sample_rate,
                    "normalized_sample_rate": normalized_rate,
                }
            },
        )

        for sub_idx, frame in enumerate(frames, start=1):
            prepared = self._prepare_playback_payload(
                frame,
                normalized_rate,
                channels=normalized_channels,
                volume=volume,
                caps=example_caps,
            )
            if prepared is None:
                self.log.warning(
                    "TTS-кадр пропущен из-за ошибки подготовки",
                    extra={
                        "attrs": {
                            "sub_index": sub_idx,
                            "frames_total": len(frames),
                        }
                    },
                )
                continue

            payload, stats = prepared

            def _builder(caps: PlaybackClientCaps) -> bytes | None:
                per_cap_pcm, per_cap_rate, per_cap_channels = _normalize_audio_for_caps(
                    frame,
                    normalized_rate,
                    normalized_channels,
                    caps,
                    resample_log=self._resample_log,
                )

                prepared_caps = self._prepare_playback_payload(
                    per_cap_pcm,
                    per_cap_rate,
                    channels=per_cap_channels,
                    volume=volume,
                    caps=caps,
                )
                return prepared_caps[0] if prepared_caps else None

            self._broadcast_payload(_builder, purpose="TTS")

            self.log.debug(
                "Сформирован TTS-кадр",
                extra={
                    "attrs": {
                        "text": text,
                        "preset": preset,
                        "chunk_index": chunk_index,
                        "chunks_total": chunks_total,
                        "sub_frame": sub_idx,
                        "sub_frames_total": len(frames),
                        **stats,
                    }
                },
            )

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
        """Отправляет фоновый звуковой эффект на робота."""

        if self._loop is None:
            self.log.warning("Event loop сервера ещё не готов, эффект не отправлен")
            return

        if not self._sessions:
            now = time.monotonic()
            if now - self._last_no_client_warning > 1.0:
                self._last_no_client_warning = now
                self.log.warning("Нет активных подключений робота для отправки эффекта")
            return

        example_caps = next(iter(self._sessions)).caps if self._sessions else PlaybackClientCaps()
        normalized_pcm, normalized_rate, normalized_channels = _normalize_audio_for_caps(
            pcm,
            sample_rate,
            channels,
            example_caps,
            resample_log=self._resample_log,
        )

        frames = self._split_pcm_for_caps(
            normalized_pcm,
            channels=normalized_channels,
            caps=example_caps,
            frame_samples_hint=None,
        )

        wire_header = 4 if example_caps.mode == "xiaozhi" else _PLAYBACK_HEADER_STRUCT.size
        self.log.info(
            "Подготовка эффекта к отправке",
            extra={
                "attrs": {
                    "frames": len(frames),
                    "frame_bytes": len(frames[0]) if frames else 0,
                    "header_bytes": wire_header,
                    "max_payload_bytes": self._max_playback_payload,
                    "pcm_bytes_total": len(normalized_pcm),
                    "effect": name,
                    "file": source_file,
                    "source_sample_rate": sample_rate,
                    "normalized_sample_rate": normalized_rate,
                }
            },
        )

        for idx, frame in enumerate(frames, start=1):
            prepared = self._prepare_playback_payload(
                frame,
                normalized_rate,
                channels=normalized_channels,
                volume=volume,
                caps=example_caps,
            )
            if prepared is None:
                self.log.warning(
                    "Эффектовый кадр пропущен из-за ошибки подготовки",
                    extra={"attrs": {"effect": name, "sub_frame": idx, "frames_total": len(frames)}},
                )
                continue
            payload, stats = prepared

            def _builder(caps: PlaybackClientCaps) -> bytes | None:
                per_cap_pcm, per_cap_rate, per_cap_channels = _normalize_audio_for_caps(
                    frame,
                    normalized_rate,
                    normalized_channels,
                    caps,
                    resample_log=self._resample_log,
                )

                prepared_caps = self._prepare_playback_payload(
                    per_cap_pcm,
                    per_cap_rate,
                    channels=per_cap_channels,
                    volume=volume,
                    caps=caps,
                )
                return prepared_caps[0] if prepared_caps else None

            self._broadcast_payload(_builder, purpose=f"эффекта {name}")

            self.log.debug(
                "Сформирован аудиокадр фонового эффекта",
                extra={
                    "attrs": {
                        "effect": name,
                        "file": source_file,
                        "repeat_index": repeat_index,
                        "repeat_total": repeat_total,
                        "volume": round(volume, 3),
                        "sub_frame": idx,
                        "frames_total": len(frames),
                        **stats,
                    }
                },
            )

    def forward_tts_chunk(
        self,
        pcm: bytes,
        sample_rate: int,
        *,
        text: str,
        preset: str,
        chunk_index: int,
        chunks_total: int,
        volume: float,
    ) -> None:
        """Совместимая с working_tts прослойка отправки TTS."""

        try:
            self.send_tts(
                pcm,
                sample_rate,
                text=text,
                preset=preset,
                chunk_index=chunk_index,
                chunks_total=chunks_total,
                volume=volume,
                channels=1,
            )
        except Exception:
            self.log.exception("Не удалось передать чанк TTS роботу")

    def forward_effect_chunk(
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
        """Проксирует звуковой эффект на робота в формате ``AP``."""

        try:
            self.send_effect(
                pcm,
                sample_rate,
                name=name,
                source_file=source_file,
                repeat_index=repeat_index,
                repeat_total=repeat_total,
                volume=volume,
                channels=channels,
            )
        except Exception:
            self.log.exception(
                "Не удалось передать звуковой эффект роботу",
                extra={"attrs": {"effect": name, "file": source_file}},
            )

    def _decode_frame(self, payload: bytes) -> RobotAudioFrame | None:
        """Разбирает бинарный пакет и сводит стерео в моно."""

        (
            magic,
            version,
            flags,
            sequence,
            timestamp_us,
            sample_rate,
            frame_samples,
            channels,
            sample_bits,
            pcm_bytes,
            rms_left,
            rms_right,
            spacing,
            direction,
            confidence,
        ) = _HEADER_STRUCT.unpack_from(payload)

        if magic != b"AF":
            self.log.warning(
                "Получен пакет с неверной сигнатурой",
                extra={"attrs": {"magic": magic}},
            )
            return None
        if version != 1:
            self.log.warning(
                "Неожиданная версия протокола",
                extra={"attrs": {"version": version}},
            )
        if sample_bits != 16:
            self.log.error(
                "Неподдерживаемая глубина сэмпла",
                extra={"attrs": {"bits": sample_bits}},
            )
            return None
        pcm_view = memoryview(payload)[_HEADER_STRUCT.size : _HEADER_STRUCT.size + pcm_bytes]
        if len(pcm_view) != pcm_bytes:
            self.log.warning(
                "Размер PCM не совпадает",
                extra={"attrs": {"expected": pcm_bytes, "actual": len(pcm_view)}},
            )
            return None
        if self._expected_sample_rate and sample_rate != self._expected_sample_rate:
            self.log.warning(
                "Неожиданная частота дискретизации",
                extra={"attrs": {"expected": self._expected_sample_rate, "actual": sample_rate}},
            )
        if self._expected_channels and channels != self._expected_channels:
            self.log.warning(
                "Неожиданное число каналов",
                extra={"attrs": {"expected": self._expected_channels, "actual": channels}},
            )

        pcm_stereo = bytes(pcm_view)
        pcm_mono = downmix_to_mono(pcm_stereo, channels)

        self.sample_rate = sample_rate
        self.frame_samples = frame_samples

        return RobotAudioFrame(
            sequence=sequence,
            timestamp_us=timestamp_us,
            sample_rate=sample_rate,
            frame_samples=frame_samples,
            channels=channels,
            sample_bits=sample_bits,
            pcm_stereo=pcm_stereo,
            pcm_mono=pcm_mono,
            rms_left=rms_left,
            rms_right=rms_right,
            mic_spacing_m=spacing,
            direction_deg=direction,
            confidence=confidence,
            localization_enabled=bool(flags & 0x01),
        )


def downmix_to_mono(pcm: bytes, channels: int) -> bytes:
    """Сводит интерливированный PCM16 в моно."""

    if channels <= 1:
        return pcm
    # Преобразуем поток байт в последовательность целых значений.
    samples = struct.unpack_from("<" + "h" * (len(pcm) // 2), pcm)
    mono: Deque[int] = deque(maxlen=len(samples) // channels)
    for idx in range(0, len(samples), channels):
        # Собираем значения, относящиеся к одному моменту времени.
        chunk = samples[idx : idx + channels]
        # Среднее значение даёт устойчивый моно-сигнал без перекосов по каналам.
        avg = int(sum(chunk) / len(chunk))
        mono.append(avg)
    # Конвертируем усреднённые значения обратно в байтовую форму PCM16.
    return struct.pack("<" + "h" * len(mono), *mono)


def _be_u16(data: bytes) -> int:
    """Читает 16-битное целое в big-endian для протокола XiaoZhi."""

    return (data[0] << 8) | data[1]


def _be_u32(data: bytes) -> int:
    """Читает 32-битное целое в big-endian для протокола XiaoZhi."""

    return (data[0] << 24) | (data[1] << 16) | (data[2] << 8) | data[3]


def _build_xiaozhi_audio_frame(
    caps: PlaybackClientCaps, payload: bytes, timestamp_ms: int | None = None
) -> bytes:
    """Упаковывает аудиоданные в BinaryProtocol2/3.

    Версия 2 содержит таймстамп, версия 3 — только тип и размер. Мы оставляем
    payload неизменным (PCM16 little-endian), чтобы прошивка могла передавать
    его напрямую в I2S без дополнительного декодирования.
    """

    if caps.xiaozhi_version == 2:
        ts = timestamp_ms or int(time.time() * 1000)
        frame = bytearray(16 + len(payload))
        # version
        frame[0] = (2 >> 8) & 0xFF
        frame[1] = 2 & 0xFF
        # type=audio
        frame[2] = 0
        frame[3] = 0
        # reserved (4..7) оставляем нулями
        frame[8] = (ts >> 24) & 0xFF
        frame[9] = (ts >> 16) & 0xFF
        frame[10] = (ts >> 8) & 0xFF
        frame[11] = ts & 0xFF
        size = len(payload)
        frame[12] = (size >> 24) & 0xFF
        frame[13] = (size >> 16) & 0xFF
        frame[14] = (size >> 8) & 0xFF
        frame[15] = size & 0xFF
        frame[16:] = payload
        return bytes(frame)

    # Версия 3: [type u8][reserved u8][size u16][payload]
    size = len(payload)
    frame = bytearray(4 + size)
    frame[0] = 0  # type=audio
    frame[1] = 0  # reserved
    frame[2] = (size >> 8) & 0xFF
    frame[3] = size & 0xFF
    frame[4:] = payload
    return bytes(frame)


def _resample_pcm(
    pcm: bytes, from_rate: int, to_rate: int, channels: int, *, log: logging.Logger
) -> bytes:
    """Выполняет линейный ресемплинг PCM16 LE между частотами.

    Нужен, чтобы TTS/эффекты из движка (например, 22.05 кГц) приходили роботу
    в точной частоте из hello XiaoZhi и не трещали из-за неверной интерпретации
    длительности кадра. Используем простой линейный интерполяционный алгоритм,
    чтобы не тянуть тяжёлые зависимости и сохранить работоспособность на ESP.
    """

    if from_rate <= 0 or to_rate <= 0:
        log.error(
            "Некорректные частоты ресемплинга",  # noqa: TRY400 — логируем и возвращаем исходник
            extra={"attrs": {"from_rate": from_rate, "to_rate": to_rate}},
        )
        return pcm

    if from_rate == to_rate or not pcm:
        return pcm

    if len(pcm) % (2 * max(1, channels)) != 0:
        log.error(
            "PCM не выровнен по каналам перед ресемплингом",
            extra={
                "attrs": {
                    "pcm_bytes": len(pcm),
                    "channels": channels,
                    "from_rate": from_rate,
                    "to_rate": to_rate,
                }
            },
        )
        return pcm

    samples = array("h")
    samples.frombytes(pcm)
    frame_count = len(samples) // max(1, channels)
    if frame_count == 0:
        return b""

    # Количество кадров после преобразования: масштабируем по отношению частот.
    target_frames = max(1, int(round(frame_count * (to_rate / from_rate))))
    ratio = from_rate / to_rate

    resampled = array("h")
    resampled.extend((0,) * (target_frames * max(1, channels)))

    for i in range(target_frames):
        src_pos = i * ratio
        left_index = int(src_pos)
        frac = src_pos - left_index
        right_index = min(left_index + 1, frame_count - 1)
        for ch in range(max(1, channels)):
            left_sample = samples[left_index * channels + ch]
            right_sample = samples[right_index * channels + ch]
            # Линейная интерполяция между соседними сэмплами.
            interpolated = int(round(left_sample + (right_sample - left_sample) * frac))
            resampled[i * channels + ch] = interpolated

    log.debug(
        "PCM отресемплирован",  # noqa: TRY400 — диагностируем качество звука
        extra={
            "attrs": {
                "from_rate": from_rate,
                "to_rate": to_rate,
                "channels": channels,
                "input_frames": frame_count,
                "output_frames": target_frames,
            }
        },
    )

    return resampled.tobytes()


def _normalize_audio_for_caps(
    pcm: bytes,
    sample_rate: int,
    channels: int,
    caps: PlaybackClientCaps,
    *,
    resample_log: logging.Logger,
) -> tuple[bytes, int, int]:
    """Приводит PCM к частоте/каналам, заявленным клиентом.

    Сначала ресемплируем к частоте из hello, затем при необходимости переводим
    в моно, чтобы XiaoZhi/AF тракт получал ожидаемое число каналов и не
    воспроизводил треск из-за неверной интерпретации кадров.
    """

    target_rate = caps.sample_rate or sample_rate
    target_channels = caps.channels or channels or 1

    normalized = pcm
    current_rate = sample_rate
    current_channels = channels or 1

    if current_rate != target_rate:
        normalized = _resample_pcm(
            normalized,
            current_rate,
            target_rate,
            max(1, current_channels),
            log=resample_log,
        )
        current_rate = target_rate

    if target_channels == 1 and current_channels != 1:
        resample_log.debug(
            "Перевожу звук в моно под формат клиента",
            extra={
                "attrs": {
                    "from_channels": current_channels,
                    "to_channels": target_channels,
                    "sample_rate": current_rate,
                }
            },
        )
        normalized = downmix_to_mono(normalized, current_channels)
        current_channels = 1

    return normalized, current_rate, current_channels
