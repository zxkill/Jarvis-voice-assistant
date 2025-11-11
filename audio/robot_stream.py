"""Приём аудио от робота по WebSocket."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import struct
from collections import deque
from typing import Deque, Set
from urllib.parse import urlparse

import websockets
from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK
from websockets.legacy.server import Serve, WebSocketServerProtocol, serve

from core.logging_json import configure_logging

_HEADER_STRUCT = struct.Struct("<2sBBIQIIHHIfffff")


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
    ) -> None:
        """Создаёт сервер, принимающий бинарные кадры PCM16 от робота."""

        self._parsed = urlparse(endpoint)
        if self._parsed.scheme not in {"ws", "wss"}:
            raise ValueError("endpoint должен начинаться с ws:// или wss://")
        self._host = self._parsed.hostname or "0.0.0.0"
        self._port = self._parsed.port or 8765
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
        self.sample_rate = expected_sample_rate
        self.frame_samples = 512
        self.log = configure_logging("audio.robot_stream")

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
        try:
            async for message in websocket:
                if isinstance(message, str):
                    self.log.debug(
                        "Текстовое сообщение от робота",
                        extra={"attrs": {"text": message}},
                    )
                    continue
                if len(message) < _HEADER_STRUCT.size:
                    self.log.warning(
                        "Получен слишком короткий пакет",
                        extra={"attrs": {"length": len(message)}},
                    )
                    continue
                frame = self._decode_frame(message)
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
            self.log.info("Соединение с роботом завершено", extra={"attrs": {"peer": peer}})

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
