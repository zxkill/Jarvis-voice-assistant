"""Приём и отправка аудио роботу по WebSocket.

Новая версия протокола следует принципу разделения управляющих сообщений
и бинарных PCM-чанков. Управление (``audio_start``/``audio_end``/``emotion``)
передаётся в текстовых JSON-фреймах, а сами сэмплы приходят и отправляются
как «сырой» байтовый поток без заголовков. Это упрощает прошивку ESP32 и
исключает проблемы с несовместимыми бинарными структурами.
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


@dataclasses.dataclass(slots=True)
class RobotAudioFrame:
    """Структура аудиокадра, который прислал робот.

    Поля оставлены максимально совместимыми с предыдущей версией, чтобы
    существующий пайплайн распознавания речи не пришлось переписывать. Там,
    где новый протокол не передаёт информацию (например, о локализации),
    заполняются безопасные значения по умолчанию.
    """

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
        max_outgoing_frame: int = 4096,
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
        # Максимальный размер одного бинарного кадра при отправке TTS/эмоций.
        # Если прислали крупный PCM-фрагмент, дробим его, чтобы не ловить
        # ошибку 1009 (Message Too Big) на стороне клиента. Нижнюю границу
        # оставляем равной 1 байту, чтобы тесты могли проверять дробление на
        # малых буферах.
        self._max_outgoing_frame = max(1, max_outgoing_frame)
        self._queue: asyncio.Queue[RobotAudioFrame | object] = asyncio.Queue(
            maxsize=max(1, queue_max)
        )
        self._expected_sample_rate = expected_sample_rate
        self._expected_channels = expected_channels
        self._server: Serve | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_event = asyncio.Event()
        self._client_tasks: Set[asyncio.Task[None]] = set()
        self._send_queues: Set[asyncio.Queue[object]] = set()
        self.sample_rate = expected_sample_rate
        self.frame_samples = 512
        # Текущие параметры входящего аудио. Их можно обновлять управляющим
        # сообщением ``mic_chunk``, чтобы сервер корректно интерпретировал
        # бинарные PCM-чанки без заголовка.
        self._incoming_sample_rate = expected_sample_rate
        self._incoming_channels = expected_channels
        self._incoming_format = "s16le"
        self._incoming_sequence = 0
        # Текущие идентификаторы исходящих потоков, чтобы отправлять
        # ``audio_start``/``audio_end`` строго по границам синтезированных
        # ответов и эффектов.
        self._current_tts_id: str | None = None
        self._current_effect_id: str | None = None
        self.log = configure_logging("audio.robot_stream")
        # Счётчик исходящих потоков, пригодный для генерации ``audio_id``.
        self._playback_sequence = 0

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
        send_queue: asyncio.Queue[object] = asyncio.Queue(maxsize=50)
        sender_task = asyncio.create_task(
            self._send_loop(websocket, send_queue, peer)
        )
        heartbeat_task = asyncio.create_task(
            self._ping_watchdog(websocket, peer)
        )
        self._send_queues.add(send_queue)
        try:
            async for message in websocket:
                if isinstance(message, str):
                    self._handle_text_message(message, peer)
                    continue
                self._handle_binary_message(message)
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
            if exc.code == 1009:
                self.log.error(
                    "Робот закрыл соединение из-за слишком большого фрейма — дроблю PCM на части",
                    extra={"attrs": {"peer": peer, "max_frame": self._max_outgoing_frame}},
                )
        except Exception:
            self.log.exception("Ошибка при обработке аудиопотока робота")
        finally:
            if task is not None:
                self._client_tasks.discard(task)
            sender_task.cancel()
            self._send_queues.discard(send_queue)
            self.log.info("Соединение с роботом завершено", extra={"attrs": {"peer": peer}})
            heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat_task

    def _handle_text_message(self, message: str, peer: str) -> None:
        """Обрабатывает управляющие JSON-сообщения от робота.

        Сейчас используется только для передачи метаданных микрофона, но
        оставлено расширяемым, чтобы в будущем принимать командные события
        (например, состояние батареи или телеметрию).
        """

        try:
            payload = json.loads(message)
        except json.JSONDecodeError:
            self.log.warning(
                "Получен невалидный JSON от робота",
                extra={"attrs": {"peer": peer, "text": message[:128]}},
            )
            return

        msg_type = payload.get("type", "")
        if msg_type == "mic_chunk":
            # Робот сообщает параметры входящего PCM, чтобы сервер правильно
            # трактовал бинарные кадры. Формат соответствует ТЗ: s16le 16 kHz.
            self._incoming_sample_rate = int(
                payload.get("sample_rate", self._incoming_sample_rate)
            )
            self._incoming_format = payload.get("sample_format", self._incoming_format)
            self._incoming_channels = int(
                payload.get("channels", self._incoming_channels)
            )
            self.log.info(
                "Обновлены параметры микрофона",
                extra={
                    "attrs": {
                        "peer": peer,
                        "sample_rate": self._incoming_sample_rate,
                        "sample_format": self._incoming_format,
                        "channels": self._incoming_channels,
                    }
                },
            )
        else:
            # Любые другие текстовые сообщения просто логируем для отладки.
            self.log.debug(
                "Текстовое сообщение от робота",
                extra={"attrs": {"peer": peer, "text": payload}},
            )

    def _handle_binary_message(self, message: bytes) -> None:
        """Принимает бинарный PCM-чанк и кладёт его в очередь для STT."""

        if not message:
            self.log.warning("Получен пустой бинарный кадр от робота")
            return
        frame = self._decode_frame(message)
        if frame is None:
            return
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
                    "bytes": len(message),
                }
            },
        )

    async def _send_loop(
        self,
        websocket: WebSocketServerProtocol,
        queue: asyncio.Queue[object],
        peer: str,
    ) -> None:
        """Отправляет накопленные чанки озвучки и управляющие сообщения на робота."""

        self.log.debug(
            "Запущен цикл отправки аудио", extra={"attrs": {"peer": peer}}
        )
        try:
            while True:
                payload = await queue.get()
                await websocket.send(payload)
                if isinstance(payload, str):
                    self.log.debug(
                        "Отправлено управляющее сообщение",
                        extra={"attrs": {"peer": peer, "payload": payload}},
                    )
                else:
                    self.log.debug(
                        "Отправлен аудиочанк роботу",
                        extra={
                            "attrs": {
                                "peer": peer,
                                "size": len(payload),
                            }
                        },
                    )
        except asyncio.CancelledError:
            self.log.debug(
                "Цикл отправки аудио остановлен", extra={"attrs": {"peer": peer}}
            )
        except Exception:
            self.log.exception(
                "Ошибка отправки аудио роботу", extra={"attrs": {"peer": peer}}
            )

    async def _ping_watchdog(
        self, websocket: WebSocketServerProtocol, peer: str
    ) -> None:
        """Отправляет ping/pong, чтобы своевременно узнавать об обрывах."""

        if not self._ping_interval or self._ping_interval <= 0:
            return
        try:
            while True:
                await asyncio.sleep(self._ping_interval)
                if websocket.closed:
                    return
                try:
                    # Отправляем ping и ждём pong не дольше таймаута.
                    pong_waiter = websocket.ping()
                    await asyncio.wait_for(
                        pong_waiter,
                        timeout=self._ping_timeout or self._ping_interval,
                    )
                    self.log.debug(
                        "Пинг-понг с роботом успешен",
                        extra={"attrs": {"peer": peer}},
                    )
                except asyncio.TimeoutError:
                    self.log.warning(
                        "Не получили pong от робота в отведённое время",
                        extra={
                            "attrs": {
                                "peer": peer,
                                "timeout": self._ping_timeout,
                                "interval": self._ping_interval,
                            }
                        },
                    )
                    await websocket.close(code=4000, reason="ping timeout")
                    return
        except asyncio.CancelledError:
            self.log.debug(
                "Пинг-понг остановлен из-за завершения подключения",
                extra={"attrs": {"peer": peer}},
            )
        except Exception:
            self.log.exception(
                "Сбой цикла ping/pong с роботом", extra={"attrs": {"peer": peer}}
            )

    def _next_playback_sequence(self) -> int:
        """Возвращает следующий номер исходящего потока аудио."""

        self._playback_sequence = (self._playback_sequence + 1) & 0xFFFFFFFF
        return self._playback_sequence

    def _broadcast_json(self, payload: dict, *, purpose: str) -> None:
        """Отправляет текстовое сообщение всем подключённым роботам."""

        if self._loop is None:
            self.log.warning(
                "Event loop сервера ещё не готов, %s не отправлен", purpose
            )
            return

        serialized = json.dumps(payload)

        def _enqueue() -> None:
            if not self._send_queues:
                self.log.warning(
                    "Нет активных подключений робота для отправки %s", purpose
                )
                return
            for queue in list(self._send_queues):
                if queue.full():
                    try:
                        _ = queue.get_nowait()
                        self.log.warning(
                            "Очередь отправки переполнена, удаляю старый кадр",
                            extra={"attrs": {"purpose": purpose}},
                        )
                    except asyncio.QueueEmpty:
                        pass
                queue.put_nowait(serialized)

        self._loop.call_soon_threadsafe(_enqueue)

    def _broadcast_binary(self, payload: bytes, *, purpose: str) -> None:
        """Отправляет бинарный PCM-чанк всем подключённым роботам."""

        if self._loop is None:
            self.log.warning(
                "Event loop сервера ещё не готов, бинарный %s не отправлен", purpose
            )
            return

        def _enqueue() -> None:
            if not self._send_queues:
                self.log.warning(
                    "Нет активных подключений робота для отправки %s", purpose
                )
                return
            for queue in list(self._send_queues):
                if queue.full():
                    try:
                        _ = queue.get_nowait()
                        self.log.warning(
                            "Очередь отправки переполнена, удаляю старый кадр",
                            extra={"attrs": {"purpose": purpose}},
                        )
                    except asyncio.QueueEmpty:
                        pass
                queue.put_nowait(payload)

        self._loop.call_soon_threadsafe(_enqueue)

    def _iter_chunked(self, payload: bytes) -> Iterable[bytes]:
        """Дробит крупный PCM на безопасные куски для WebSocket-клиента."""

        if not payload:
            return []
        size = self._max_outgoing_frame
        # Используем генератор, чтобы не создавать лишних копий.
        return (payload[idx : idx + size] for idx in range(0, len(payload), size))

    def _send_audio_start(
        self,
        audio_id: str,
        *,
        kind: str,
        sample_rate: int,
        sample_format: str,
        channels: int,
        length_ms: float | None = None,
    ) -> None:
        """Отправляет управляющее сообщение ``audio_start`` перед PCM-потоком."""

        message: dict[str, object] = {
            "type": "audio_start",
            "id": audio_id,
            "kind": kind,
            "sample_rate": sample_rate,
            "sample_format": sample_format,
            "channels": channels,
        }
        if length_ms is not None:
            message["length_ms"] = int(length_ms)
        self.log.info(
            "Начинаю поток аудио",
            extra={"attrs": {"audio_id": audio_id, "kind": kind}},
        )
        self._broadcast_json(message, purpose=f"audio_start/{kind}")

    def _send_audio_end(self, audio_id: str) -> None:
        """Завершает поток озвучки сообщением ``audio_end``."""

        self.log.info(
            "Завершаю поток аудио", extra={"attrs": {"audio_id": audio_id}}
        )
        self._broadcast_json({"type": "audio_end", "id": audio_id}, purpose="audio_end")

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
        """Отправляет подготовленный PCM на робота через WebSocket.

        Поток TTS сопровождается управляющими сообщениями ``audio_start`` и
        ``audio_end``. Первая часть отправляется при получении первого чанка,
        финальная — после последнего. Так прошивка ESP32 может паузить микрофон
        и не тратить ресурсы на разбор заголовков.
        """

        if self._loop is None:
            self.log.warning("Event loop сервера ещё не готов, TTS не отправлен")
            return

        if chunk_index <= 1 or self._current_tts_id is None:
            if self._current_tts_id is not None:
                self._send_audio_end(self._current_tts_id)
            self._current_tts_id = f"tts-{self._next_playback_sequence()}-{int(time.time()*1000)}"
            self._send_audio_start(
                self._current_tts_id,
                kind="tts",
                sample_rate=sample_rate,
                sample_format="s16le",
                channels=channels,
            )

        # Дробим крупный PCM на фреймы, чтобы WebSocket-клиент ESP32 не рвал соединение с кодом 1009.
        for piece_index, piece in enumerate(self._iter_chunked(pcm), start=1):
            self._broadcast_binary(piece, purpose="tts_pcm")
            duration_ms = (
                len(piece) / (2 * max(1, channels) * sample_rate) * 1000
                if sample_rate > 0
                else 0.0
            )
            self.log.debug(
                "Отправлен TTS-чанк",
                extra={
                    "attrs": {
                        "audio_id": self._current_tts_id,
                        "text": text,
                        "preset": preset,
                        "chunk_index": chunk_index,
                        "chunks_total": chunks_total,
                        "volume": round(volume, 3),
                        "pcm_bytes": len(piece),
                        "duration_ms": round(duration_ms, 2),
                        "split_index": piece_index,
                        "split_total": math.ceil(len(pcm) / self._max_outgoing_frame),
                    }
                },
            )

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
        """Отправляет фоновый звуковой эффект на робота.

        Использует тот же протокол ``audio_start``/``audio_end``. Эффекты могут
        идти подряд, поэтому при первом чанке каждого повторения отправляем
        новый идентификатор, а при последнем — сигнал завершения.
        """

        if self._loop is None:
            self.log.warning("Event loop сервера ещё не готов, эффект не отправлен")
            return

        if repeat_index <= 1 or self._current_effect_id is None:
            if self._current_effect_id is not None:
                self._send_audio_end(self._current_effect_id)
            self._current_effect_id = (
                f"emotion-{self._next_playback_sequence()}-{int(time.time()*1000)}"
            )
            self._send_audio_start(
                self._current_effect_id,
                kind="emotion",
                sample_rate=sample_rate,
                sample_format="s16le",
                channels=channels,
            )

        for piece_index, piece in enumerate(self._iter_chunked(pcm), start=1):
            self._broadcast_binary(piece, purpose="effect_pcm")
            self.log.debug(
                "Отправлен аудиочанк фонового эффекта",
                extra={
                    "attrs": {
                        "effect": name,
                        "file": source_file,
                        "repeat_index": repeat_index,
                        "repeat_total": repeat_total,
                        "pcm_bytes": len(piece),
                        "audio_id": self._current_effect_id,
                        "volume": round(volume, 3),
                        "split_index": piece_index,
                        "split_total": math.ceil(len(pcm) / self._max_outgoing_frame),
                    }
                },
            )

        if repeat_index >= repeat_total and self._current_effect_id is not None:
            self._send_audio_end(self._current_effect_id)
            self._current_effect_id = None

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
        """Проксирует звуковой эффект на робота в новом протоколе PCM."""

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
        """Разбирает бинарный PCM-чанк и вычисляет полезные метрики."""

        if len(payload) % 2 != 0:
            self.log.warning(
                "Размер PCM не делится на 2 байта (16-bit)",
                extra={"attrs": {"pcm_bytes": len(payload)}},
            )
            return None

        channels = max(1, self._incoming_channels or 1)
        sample_rate = self._incoming_sample_rate or self._expected_sample_rate
        if self._incoming_format.lower() != "s16le":
            self.log.warning(
                "Неподдерживаемый формат сэмпла",
                extra={"attrs": {"format": self._incoming_format}},
            )
            return None

        if channels > 1 and (len(payload) // 2) % channels != 0:
            self.log.error(
                "Размер PCM не делится на количество каналов",
                extra={"attrs": {"pcm_bytes": len(payload), "channels": channels}},
            )
            return None

        samples = array("h")
        samples.frombytes(payload)

        # Считаем RMS по каналам для мониторинга качества.
        if channels == 1:
            rms_val = math.sqrt(sum(val * val for val in samples) / len(samples)) if samples else 0.0
            rms_left = rms_right = rms_val
            pcm_mono = payload
            pcm_stereo = payload
        else:
            left_vals = samples[0::channels]
            right_vals = samples[1::channels]
            rms_left = math.sqrt(sum(val * val for val in left_vals) / len(left_vals)) if left_vals else 0.0
            rms_right = math.sqrt(sum(val * val for val in right_vals) / len(right_vals)) if right_vals else 0.0
            pcm_stereo = payload
            pcm_mono = downmix_to_mono(payload, channels)

        frame_samples = len(pcm_mono) // 2
        self.sample_rate = sample_rate
        self.frame_samples = frame_samples
        self._incoming_sequence = (self._incoming_sequence + 1) & 0xFFFFFFFF

        return RobotAudioFrame(
            sequence=self._incoming_sequence,
            timestamp_us=int(time.time() * 1_000_000),
            sample_rate=sample_rate,
            frame_samples=frame_samples,
            channels=channels,
            sample_bits=16,
            pcm_stereo=pcm_stereo,
            pcm_mono=pcm_mono,
            rms_left=rms_left,
            rms_right=rms_right,
            mic_spacing_m=0.0,
            direction_deg=0.0,
            confidence=0.0,
            localization_enabled=False,
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
