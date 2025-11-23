"""Клиент для обращения к облачному агенту Xiaozhi.

Модуль изолирует сетевое общение с сервисом, чтобы легко переключать
бэкенды LLM через конфигурацию. Запросы и ответы подробно логируются,
в коде добавлены русские комментарии для быстрой поддержки.
"""

from __future__ import annotations

import importlib
import io
import json
import logging
import socket
import wave
import platform
import uuid
from dataclasses import dataclass
import re
from typing import Any, Dict, Optional
from urllib.parse import urlparse, urlunparse

import requests
from websockets.sync.client import connect

logger = logging.getLogger(__name__)


@dataclass
class XiaozhiSettings:
    """Настройки соединения с облачным агентом."""

    endpoint: str
    agent_code: str
    device_id: str = "jarvis-client"
    client_id: str | None = None
    manager_url: str = ""
    manager_secret: str = ""
    timeout: float = 40.0


class XiaozhiBindingRequired(RuntimeError):
    """Исключение, сигнализирующее о необходимости привязки устройства."""

    def __init__(self, bind_code: str):
        super().__init__(f"Требуется привязать устройство, код: {bind_code}")
        self.bind_code = bind_code


class XiaozhiClient:
    """Клиент Xiaozhi с поддержкой HTTP и WebSocket протоколов.

    По умолчанию пытаемся выбрать подходящий транспорт по схеме URL:
    ``http/https`` → обычный POST, ``ws/wss`` → WebSocket-сеанс. Это приближает
    поведение к прошивке ESP32, где основное общение с моделью идёт через
    WebSocket, но сохраняет обратную совместимость с HTTP‑прокси.
    """

    # Настройки аудио, совпадающие с официальной прошивкой ESP32: Opus 16 kHz,
    # моно, длительность кадра 60 мс. Значения используются и при кодировании
    # исходного WAV, и при декодировании бинарных ответов в PCM/WAV.
    OPUS_SAMPLE_RATE = 16000
    OPUS_FRAME_DURATION_MS = 60
    OPUS_FRAME_SIZE = int(OPUS_SAMPLE_RATE * (OPUS_FRAME_DURATION_MS / 1000))

    def __init__(self, settings: XiaozhiSettings) -> None:
        self.settings = settings
        # Чтобы не опрашивать manager-api перед каждым запросом, запоминаем,
        # что проверка привязки уже выполнялась в рамках жизни объекта.
        self._binding_checked = False
        # Если устройство не указано в конфиге, пытаемся определить MAC-адрес
        # реального сетевого адаптера. Это повторяет подход py-xiaozhi и
        # официальной прошивки: сервер ожидает узнаваемый идентификатор, а
        # рандомные строки ("jarvis-client") приводят к закрытию соединения
        # без hello. MAC дополнительно используется как client_id.
        if not self.settings.device_id or self.settings.device_id == "jarvis-client":
            self.settings.device_id = self._get_mac_address()

        # Клиент/устройство часто совпадают, поэтому задаём падение до
        # device_id, чтобы заголовок Client-Id всегда присутствовал, как в
        # прошивке ESP32 и py-xiaozhi.
        if not self.settings.client_id:
            self.settings.client_id = self.settings.device_id

        # Загрузка Opus откладывается до первого аудио-запроса. Так сервис
        # сможет стартовать даже при отсутствии зависимости, а в логах будет
        # понятная ошибка о необходимости установить `opuslib` перед
        # использованием голосового моста.
        self._opus_encoder_cls = None
        self._opus_decoder_cls = None

    def _ensure_opus_loaded(self) -> tuple[type, type]:
        """Лениво загрузить классы Encoder/Decoder из ``opuslib``."""

        if self._opus_encoder_cls and self._opus_decoder_cls:
            return self._opus_encoder_cls, self._opus_decoder_cls

        import importlib

        spec = importlib.util.find_spec("opuslib")
        if spec is None:
            raise RuntimeError(
                "Отсутствует зависимость opuslib: установите её из requirements.txt перед использованием Xiaozhi аудио"
            )

        opus_module = importlib.import_module("opuslib")
        self._opus_encoder_cls = opus_module.Encoder
        self._opus_decoder_cls = opus_module.Decoder
        logger.debug("Библиотека opuslib загружена лениво", extra={"encoder": str(self._opus_encoder_cls)})
        return self._opus_encoder_cls, self._opus_decoder_cls

    @staticmethod
    def _get_mac_address() -> str:
        """Получить MAC-адрес устройства в формате XX:XX:XX:XX:XX:XX.

        Используем `uuid.getnode()`, который стабильно работает без внешних
        зависимостей. Если MAC не удаётся извлечь (редко на виртуалках),
        возвращаем безопасный fallback, но логируем предупреждение, чтобы
        администратор мог вручную прописать device_id в конфиге.
        """

        mac_int = uuid.getnode()
        if (mac_int >> 40) % 2:
            logger.warning("uuid.getnode() вернул локально сгенерированный MAC, используем как fallback")
        mac_str = ":".join(f"{(mac_int >> ele) & 0xFF:02x}" for ele in range(40, -1, -8))
        logger.info("Определён MAC-адрес устройства", extra={"device_id": mac_str})
        return mac_str

    def _build_auth_headers(self) -> Dict[str, str]:
        """Собрать авторизационные заголовки с безопасной обработкой префикса.

        Сервер ожидает заголовок ``Authorization`` с префиксом ``Bearer`` и
        вспомогательный ``X-Share-Code``. Чтобы не заставлять пользователя
        вручную добавлять префикс, делаем это автоматически.
        """

        if not self.settings.agent_code:
            return {}

        token = self.settings.agent_code
        if " " not in token:
            token = f"Bearer {token}"

        return {
            "Authorization": token,
            "X-Share-Code": self.settings.agent_code,
            # Совместимость с py-xiaozhi: сервер читает идентификаторы из
            # заголовков, поэтому дублируем здесь, чтобы handshake совпал.
            "Device-Id": self.settings.device_id,
            "Client-Id": self.settings.client_id or self.settings.device_id,
            "Protocol-Version": "1",
            "User-Agent": f"Jarvis-xiaozhi/{platform.system()}-{platform.release()}",
        }

    def _request_bind_code(self, trace_id: str = "") -> Optional[str]:
        """Запросить код привязки устройства через manager-api.

        В прошивке ESP32 для неподключённых устройств сервер возвращает
        бизнес-код ``10042`` и шестизначный цифровой код, который пользователь
        вводит на сайте xiaozhi.me. Здесь повторяем ту же логику, чтобы
        настольный Jarvis мог вывести этот код вместо немой ошибки.
        """

        if not self.settings.manager_url or not self.settings.manager_secret:
            # Консоль не подключена — пропускаем попытку, чтобы не ломать
            # стандартный поток общения через WebSocket/HTTP.
            return None

        payload = {
            "macAddress": self.settings.device_id,
            "clientId": self.settings.client_id or self.settings.device_id,
            "selectedModule": {"LLM": "xiaozhi", "ASR": "", "TTS": ""},
        }
        headers = {
            "Authorization": f"Bearer {self.settings.manager_secret}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        url = self.settings.manager_url.rstrip("/") + "/config/agent-models"
        logger.info(
            "Запрашиваем код привязки Xiaozhi", extra={"trace_id": trace_id, "url": url}
        )
        try:
            response = requests.post(
                url, json=payload, headers=headers, timeout=self.settings.timeout
            )
        except requests.RequestException as exc:  # pragma: no cover - сетевые сбои
            logger.warning("Не удалось обратиться к manager-api Xiaozhi: %s", exc)
            return None

        try:
            data = response.json()
        except ValueError:
            logger.error(
                "manager-api Xiaozhi вернул невалидный JSON", extra={"body": response.text}
            )
            return None

        code = data.get("code")
        if code == 0:
            logger.debug(
                "manager-api подтвердил привязку устройства", extra={"trace_id": trace_id}
            )
            return None

        if code == 10042:
            # Сообщение обычно содержит сам цифровой код — извлекаем цифры, чтобы
            # гарантировать корректный формат для озвучки и отображения.
            raw_msg = str(data.get("msg", ""))
            digits = re.findall(r"\d", raw_msg)
            bind_code = "".join(digits[:6]) if digits else raw_msg.strip()
            logger.warning(
                "Сервер требует привязки устройства", extra={"bind_code": bind_code, "trace_id": trace_id}
            )
            return bind_code

        # Для кодов 10041 (устройство не найдено) и других — просто логируем
        # и продолжаем штатный процесс, чтобы не блокировать работу.
        logger.info(
            "manager-api вернул код %s, продолжаем без привязки", code, extra={"trace_id": trace_id}
        )
        return None

    def _build_payload(self, prompt: str) -> Dict[str, Any]:
        """Сформировать JSON‑тело запроса.

        На большинстве сайтов Xiaozhi ожидает два ключа: share code агента и
        входной текст. Выносим подготовку в отдельный метод, чтобы упростить
        тестирование и последующую адаптацию под изменения API.
        """

        return {
            "share_code": self.settings.agent_code,
            "input": prompt,
            "stream": False,
        }

    def _build_hello_message(self) -> str:
        """Сформировать hello для WebSocket по официальной схеме.

        Оригинальная прошивка шлёт минимальный набор полей: тип сообщения,
        версию протокола, поддержку MCP и параметры аудио (Opus, 16 kHz, mono,
        60 мс). Идентификаторы устройства и токен идут строго в заголовках
        WebSocket, поэтому не передаём их в body, чтобы не нарушать серверную
        проверку формата (разрыв с кодом 1005 без ответа).
        """

        return json.dumps(
            {
                "type": "hello",
                "version": 1,
                "features": {"mcp": True},
                "transport": "websocket",
                "audio_params": {
                    "format": "opus",
                    "sample_rate": self.OPUS_SAMPLE_RATE,
                    "channels": 1,
                    "frame_duration": self.OPUS_FRAME_DURATION_MS,
                },
            }
        )

    def _expect_server_hello(self, ws, *, trace_id: str = "", timeout: float | None = 5) -> Dict[str, Any]:
        """Дождаться корректного server hello или упасть с осмысленной ошибкой."""

        try:
            message = ws.recv(timeout=timeout)
        except Exception as exc:  # pragma: no cover - сетевые особенности
            logger.error(
                "Не получили hello от Xiaozhi: %s", exc, extra={"trace_id": trace_id}
            )
            raise RuntimeError("Xiaozhi разорвал соединение без hello") from exc

        if message is None:
            logger.error("Xiaozhi прислал пустой hello", extra={"trace_id": trace_id})
            raise RuntimeError("Xiaozhi прислал пустой hello")

        if isinstance(message, bytes):
            logger.error(
                "Xiaozhi прислал бинарный hello", extra={"trace_id": trace_id, "bytes": len(message)}
            )
            raise RuntimeError("Xiaozhi прислал бинарный hello")

        try:
            data = json.loads(message)
        except ValueError as exc:
            logger.error("Некорректный JSON hello Xiaozhi: %s", message, extra={"trace_id": trace_id})
            raise RuntimeError("Некорректный hello от Xiaozhi") from exc

        if not isinstance(data, dict) or data.get("type") != "hello":
            logger.error("Не получили корректный hello от Xiaozhi", extra={"trace_id": trace_id, "data": data})
            raise RuntimeError("Xiaozhi не вернул hello")

        logger.info("Xiaozhi вернул hello", extra={"trace_id": trace_id})
        return data

    def ask(self, prompt: str, *, trace_id: str = "") -> str:
        """Отправить запрос в Xiaozhi и вернуть текстовый ответ.

        При сетевой ошибке или пустом ответе возбуждается :class:`RuntimeError`.
        """

        # Перед обращением к основному API пробуем узнать, не требует ли сервер
        # предварительной привязки устройства. Код привязки показываем сразу,
        # чтобы пользователь мог ввести его в консоли и повторить запрос.
        if not self._binding_checked:
            self._binding_checked = True
            bind_code = self._request_bind_code(trace_id=trace_id)
            if bind_code:
                raise XiaozhiBindingRequired(bind_code)

        if not self.settings.agent_code:
            logger.error("Не задан xiaozhi_agent_code, WebSocket недоступен", extra={"trace_id": trace_id})
            raise RuntimeError("Укажите xiaozhi_agent_code в конфиге")

        if self.settings.endpoint.startswith(("ws://", "wss://")):
            return self._ask_websocket(prompt, trace_id=trace_id)

        payload = self._build_payload(prompt)
        headers = {
            "Content-Type": "application/json",
            "device-id": self.settings.device_id,
            "client-id": self.settings.client_id,
        }
        headers.update(self._build_auth_headers())
        if trace_id:
            headers["X-Trace-Id"] = trace_id

        logger.debug(
            "Отправка запроса в Xiaozhi", extra={"endpoint": self.settings.endpoint, "trace_id": trace_id}
        )

        try:
            response = requests.post(
                self.settings.endpoint,
                json=payload,
                headers=headers,
                timeout=self.settings.timeout,
            )
        except requests.RequestException as exc:  # pragma: no cover - сетевые сбои вне тестов
            logger.error("Сервис Xiaozhi недоступен: %s", exc)
            raise RuntimeError("Не удалось связаться с Xiaozhi") from exc

        # Если HTTP‑путь не найден, пробуем автоматически переключиться на WebSocket
        if not response.ok and response.status_code == 404:
            ws_endpoint = self._derive_ws_endpoint()
            if ws_endpoint:
                logger.warning(
                    "HTTP путь Xiaozhi вернул 404, пробуем WebSocket",
                    extra={"endpoint": ws_endpoint, "trace_id": trace_id},
                )
                return self._ask_websocket(prompt, trace_id=trace_id, override_endpoint=ws_endpoint)

        if not response.ok:
            logger.error(
                "Xiaozhi вернул ошибку", extra={"status": response.status_code, "body": response.text}
            )
            raise RuntimeError(f"Сервис Xiaozhi вернул {response.status_code}")

        try:
            data: Dict[str, Any] = response.json()
        except ValueError as exc:
            logger.error("Некорректный JSON от Xiaozhi: %s", response.text)
            raise RuntimeError("Xiaozhi вернул невалидный JSON") from exc

        # Пытаемся аккуратно извлечь текстовую часть ответа
        text = self._extract_text(data)
        if not text:
            logger.error("Пустой ответ от Xiaozhi", extra={"raw": data})
            raise RuntimeError("Xiaozhi не вернул текст ответа")

        logger.info("Ответ Xiaozhi длиной %d символов", len(text), extra={"trace_id": trace_id})
        return text

    def ask_audio(self, wav_bytes: bytes, *, trace_id: str = "") -> bytes:
        """Отправить аудио в Xiaozhi и вернуть аудио-ответ.

        Этот путь нужен для Telegram, где мы имитируем голосовой обмен: TTS →
        Xiaozhi → TTS. Формат запроса — mono WAV 16-bit. Если HTTP-путь отвечает
        404, автоматически переключаемся на WebSocket и пытаемся повторить
        бинарный обмен.
        """

        if not self._binding_checked:
            self._binding_checked = True
            bind_code = self._request_bind_code(trace_id=trace_id)
            if bind_code:
                raise XiaozhiBindingRequired(bind_code)

        if not self.settings.agent_code:
            logger.error("Не задан xiaozhi_agent_code, WebSocket аудио недоступен", extra={"trace_id": trace_id})
            raise RuntimeError("Укажите xiaozhi_agent_code в конфиге для аудио")

        headers = {
            "Content-Type": "audio/wav",
            "device-id": self.settings.device_id,
            "client-id": self.settings.client_id,
        }
        headers.update(self._build_auth_headers())
        if trace_id:
            headers["X-Trace-Id"] = trace_id

        logger.debug(
            "Отправка аудио-запроса в Xiaozhi", extra={"endpoint": self.settings.endpoint, "bytes": len(wav_bytes), "trace_id": trace_id}
        )

        if self.settings.endpoint.startswith(("ws://", "wss://")):
            return self._ask_websocket_audio(wav_bytes, trace_id=trace_id)

        try:
            response = requests.post(
                self.settings.endpoint,
                data=wav_bytes,
                headers=headers,
                timeout=self.settings.timeout,
            )
        except requests.RequestException as exc:  # pragma: no cover - сетевые сбои
            logger.error("Сервис Xiaozhi недоступен (аудио): %s", exc)
            raise RuntimeError("Не удалось связаться с Xiaozhi") from exc

        if not response.ok and response.status_code == 404:
            ws_endpoint = self._derive_ws_endpoint()
            if ws_endpoint:
                logger.warning(
                    "HTTP аудио-путь Xiaozhi вернул 404, пробуем WebSocket",
                    extra={"endpoint": ws_endpoint, "trace_id": trace_id},
                )
                return self._ask_websocket_audio(
                    wav_bytes, trace_id=trace_id, override_endpoint=ws_endpoint
                )

        if not response.ok:
            logger.error(
                "Xiaozhi вернул ошибку на аудио-запрос", extra={"status": response.status_code, "body": response.text}
            )
            raise RuntimeError(f"Сервис Xiaozhi вернул {response.status_code}")

        logger.info(
            "Получен аудио-ответ от Xiaozhi (HTTP)", extra={"bytes": len(response.content), "trace_id": trace_id}
        )
        return response.content

    def _ask_websocket(self, prompt: str, *, trace_id: str = "", override_endpoint: Optional[str] = None) -> str:
        """Отправить текст в Xiaozhi через WebSocket и дождаться ответа.

        Мы повторяем базовый handshake из документации проекта: открываем сессию,
        отправляем единичное текстовое сообщение с полями ``share_code`` и
        ``input`` и читаем первые осмысленные данные в ответ. Формат ответа
        совпадает с HTTP‑веткой, поэтому повторно используем извлечение текста.
        """

        headers = {
            "device-id": self.settings.device_id,
            "client-id": self.settings.client_id,
            # Версия протокола взята из прошивки ESP32 и используется сервером
            # при подборе бинарного формата.
            "Protocol-Version": "1",
        }
        headers.update(self._build_auth_headers())
        if trace_id:
            headers["X-Trace-Id"] = trace_id

        payload = json.dumps(self._build_payload(prompt))
        logger.debug(
            "Устанавливаем WebSocket сессию Xiaozhi",
            extra={"endpoint": override_endpoint or self.settings.endpoint, "trace_id": trace_id},
        )

        try:
            ws_endpoint = override_endpoint or self.settings.endpoint
            with connect(
                ws_endpoint,
                additional_headers=headers,
                open_timeout=self.settings.timeout,
                close_timeout=self.settings.timeout,
            ) as ws:
                # Отправляем приветствие по аналогии с прошивкой: без него сервер
                # может закрыть соединение кодом 1005, не дожидаясь текста.
                ws.send(self._build_hello_message())
                server_hello = self._expect_server_hello(ws, trace_id=trace_id)

                # После успешного handshake отправляем полезную нагрузку.
                ws.send(payload)
                # Читаем первые несколько сообщений, собирая содержимое
                accumulated = ""
                while True:
                    try:
                        message = ws.recv(timeout=self.settings.timeout)
                    except TimeoutError as exc:  # pragma: no cover - сетевые таймауты вне тестов
                        logger.error("Xiaozhi WebSocket timeout", extra={"trace_id": trace_id})
                        raise RuntimeError("Таймаут ожидания ответа от Xiaozhi") from exc
                    except (ConnectionError, socket.error) as exc:  # pragma: no cover - сетевые сбои
                        logger.error("Xiaozhi WebSocket connection error: %s", exc, extra={"trace_id": trace_id})
                        raise RuntimeError("Соединение с Xiaozhi разорвано") from exc

                    if message is None:
                        break

                    if isinstance(message, bytes):
                        logger.debug("Пропускаем бинарный кадр длиной %d", len(message), extra={"trace_id": trace_id})
                        continue

                    try:
                        data = json.loads(message)
                    except ValueError:
                        logger.warning("Некорректный JSON кадр Xiaozhi: %s", message, extra={"trace_id": trace_id})
                        continue

                    # В WebSocket-ответах может прийти служебный hello — сохранённый
                    # выше handshake уже подтвердил соединение, поэтому лишние hello
                    # просто логируем и пропускаем.
                    if isinstance(data, dict) and data.get("type") == "hello":
                        logger.info("Xiaozhi подтвердил повторный hello", extra={"trace_id": trace_id})
                        continue

                    chunk = self._extract_text(data)
                    if chunk:
                        accumulated += (" " if accumulated else "") + chunk
                        logger.debug(
                            "Получен текстовый фрагмент Xiaozhi", extra={"len": len(chunk), "trace_id": trace_id}
                        )

                    # Если сервис прислал флажок об окончании генерации, прекращаем чтение
                    if isinstance(data, dict) and data.get("done") is True:
                        break

                if not accumulated:
                    logger.error("Пустой ответ от Xiaozhi по WebSocket", extra={"trace_id": trace_id})
                    raise RuntimeError("Xiaozhi не вернул текст ответа")

                logger.info(
                    "Ответ Xiaozhi (WebSocket) длиной %d символов", len(accumulated), extra={"trace_id": trace_id}
                )
                return accumulated
        except Exception as exc:
            logger.error("Ошибка WebSocket Xiaozhi: %s", exc, extra={"trace_id": trace_id})
            raise RuntimeError("Не удалось получить ответ от Xiaozhi по WebSocket") from exc

    @staticmethod
    def _read_wav_pcm(wav_bytes: bytes, *, trace_id: str = "") -> tuple[bytes, int]:
        """Извлечь PCM и sample rate из WAV-байтов с подробным логом."""

        with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
            sample_rate = wf.getframerate()
            pcm = wf.readframes(wf.getnframes())
            channels = wf.getnchannels()
            sampwidth = wf.getsampwidth()

        logger.debug(
            "Разобрали WAV для Xiaozhi",
            extra={"trace_id": trace_id, "rate": sample_rate, "channels": channels, "width": sampwidth},
        )
        if channels != 1 or sampwidth != 2:
            logger.warning(
                "WAV не mono/16-bit, сервер Xiaozhi ожидает mono PCM",
                extra={"trace_id": trace_id, "channels": channels, "width": sampwidth},
            )
        return pcm, sample_rate

    def _encode_wav_to_opus(self, wav_bytes: bytes, *, trace_id: str = "") -> list[bytes]:
        """Преобразовать WAV в список Opus-кадров длительностью 60 мс."""

        pcm, sample_rate = self._read_wav_pcm(wav_bytes, trace_id=trace_id)
        if sample_rate != self.OPUS_SAMPLE_RATE:
            logger.warning(
                "Частота WAV %s Hz не совпадает с 16 kHz, Opus будет пересчитывать",
                sample_rate,
                extra={"trace_id": trace_id},
            )

        encoder_cls, _ = self._ensure_opus_loaded()
        encoder = encoder_cls(self.OPUS_SAMPLE_RATE, 1, application="audio")
        frame_bytes = self.OPUS_FRAME_SIZE * 2  # 16-bit mono → 2 байта на сэмпл
        opus_frames: list[bytes] = []
        for offset in range(0, len(pcm), frame_bytes):
            frame = pcm[offset : offset + frame_bytes]
            if len(frame) < frame_bytes:
                # Добиваем тишиной, чтобы последний кадр не был урезан сервером
                frame = frame.ljust(frame_bytes, b"\x00")
            encoded = encoder.encode(frame, self.OPUS_FRAME_SIZE)
            opus_frames.append(encoded)
            logger.debug(
                "Сформирован Opus-кадр для Xiaozhi",
                extra={"trace_id": trace_id, "frame_len": len(encoded), "offset": offset},
            )

        logger.info(
            "Всего подготовлено %d Opus-кадров для Xiaozhi",
            len(opus_frames),
            extra={"trace_id": trace_id},
        )
        return opus_frames

    def _opus_frames_to_wav(self, frames: list[bytes], *, trace_id: str = "") -> bytes:
        """Собрать WAV из списка Opus-кадров для дальнейшей транскрипции."""

        _, decoder_cls = self._ensure_opus_loaded()
        decoder = decoder_cls(self.OPUS_SAMPLE_RATE, 1)
        pcm_chunks: list[bytes] = []
        for index, frame in enumerate(frames):
            try:
                pcm = decoder.decode(frame, self.OPUS_FRAME_SIZE)
                pcm_chunks.append(pcm)
                logger.debug(
                    "Декодирован Opus-кадр Xiaozhi",
                    extra={"trace_id": trace_id, "index": index, "pcm_len": len(pcm)},
                )
            except Exception as exc:  # pragma: no cover - декодер может упасть на битых данных
                logger.error(
                    "Ошибка декодирования Opus-кадра Xiaozhi: %s",
                    exc,
                    extra={"trace_id": trace_id, "index": index, "frame_len": len(frame)},
                )
                continue

        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(self.OPUS_SAMPLE_RATE)
            wf.writeframes(b"".join(pcm_chunks))

        wav = buffer.getvalue()
        logger.info(
            "Собран WAV из %d Opus-кадров Xiaozhi", len(frames), extra={"trace_id": trace_id, "bytes": len(wav)}
        )
        return wav

    def _ask_websocket_audio(
        self, wav_bytes: bytes, *, trace_id: str = "", override_endpoint: Optional[str] = None
    ) -> bytes:
        """Передать аудио в Xiaozhi по WebSocket и вернуть бинарный ответ."""

        opus_frames = self._encode_wav_to_opus(wav_bytes, trace_id=trace_id)
        if not opus_frames:
            logger.error("Не удалось подготовить Opus кадры для Xiaozhi", extra={"trace_id": trace_id})
            raise RuntimeError("Не удалось подготовить аудио для Xiaozhi")

        headers = {
            "device-id": self.settings.device_id,
            "client-id": self.settings.client_id,
            "Protocol-Version": "1",
        }
        headers.update(self._build_auth_headers())
        if trace_id:
            headers["X-Trace-Id"] = trace_id

        endpoint = override_endpoint or self.settings.endpoint
        total_opus_bytes = sum(len(frame) for frame in opus_frames)
        logger.debug(
            "Устанавливаем WebSocket для аудио Xiaozhi",
            extra={"endpoint": endpoint, "trace_id": trace_id, "bytes": total_opus_bytes},
        )

        try:
            with connect(
                endpoint,
                additional_headers=headers,
                open_timeout=self.settings.timeout,
                close_timeout=self.settings.timeout,
            ) as ws:
                ws.send(self._build_hello_message())
                server_hello = self._expect_server_hello(ws, trace_id=trace_id)

                session_id = server_hello.get("session_id", "") if isinstance(server_hello, dict) else ""
                if session_id:
                    listen = json.dumps(
                        {"session_id": session_id, "type": "listen", "state": "start", "mode": "manual"}
                    )
                    ws.send(listen)

                # Основной аудио-трафик отправляем кадр за кадром Opus, как в прошивке ESP32.
                for idx, frame in enumerate(opus_frames):
                    ws.send(frame)
                    logger.debug(
                        "Отправлен Opus-кадр Xiaozhi", extra={"trace_id": trace_id, "index": idx, "len": len(frame)}
                    )

                chunks: list[bytes] = []
                while True:
                    try:
                        message = ws.recv(timeout=self.settings.timeout)
                    except TimeoutError as exc:  # pragma: no cover - таймауты сети
                        logger.error(
                            "Таймаут ожидания аудио-ответа Xiaozhi", extra={"trace_id": trace_id}
                        )
                        raise RuntimeError("Таймаут ожидания аудио-ответа Xiaozhi") from exc
                    except (ConnectionError, socket.error) as exc:  # pragma: no cover
                        logger.error("WebSocket аудио Xiaozhi разорван: %s", exc, extra={"trace_id": trace_id})
                        raise RuntimeError("Соединение с Xiaozhi разорвано") from exc

                    if message is None:
                        break
                    if isinstance(message, bytes):
                        chunks.append(message)
                        logger.debug(
                            "Получен аудио-чанк от Xiaozhi", extra={"trace_id": trace_id, "len": len(message)}
                        )
                        continue
                    try:
                        data = json.loads(message)
                    except ValueError:
                        logger.warning(
                            "Некорректный JSON кадр (аудио) Xiaozhi: %s", message, extra={"trace_id": trace_id}
                        )
                        continue
                    if isinstance(data, dict) and data.get("done"):
                        break

                if not chunks:
                    logger.error("Пустой аудио-ответ от Xiaozhi", extra={"trace_id": trace_id})
                    raise RuntimeError("Xiaozhi не вернул аудио")

                wav_response = self._opus_frames_to_wav(chunks, trace_id=trace_id)
                logger.info(
                    "Получен аудио-ответ от Xiaozhi (WebSocket)", extra={"bytes": len(wav_response), "trace_id": trace_id}
                )
                return wav_response
        except Exception as exc:
            logger.error("Ошибка WebSocket Xiaozhi (аудио): %s", exc, extra={"trace_id": trace_id})
            raise RuntimeError("Не удалось получить аудио от Xiaozhi по WebSocket") from exc

    def _derive_ws_endpoint(self) -> Optional[str]:
        """Попробовать вычислить WebSocket‑адрес из HTTP URL.

        Если видим официальный домен ``api.tenclass.net``, строим канонический
        путь ``/xiaozhi/v1/`` — он прописан в исходниках сервера и в OTA
        распаковке клиента. Для остальных адресов сохраняем мягкую схему
        http→ws и добавляем суффикс ``/ws`` для обратной совместимости с
        самодельными прокси.
        """

        parsed = urlparse(self.settings.endpoint)
        if parsed.scheme not in {"http", "https"}:
            return None

        ws_scheme = "wss" if parsed.scheme == "https" else "ws"

        # Специальный кейс: официальный CDN api.tenclass.net публикует
        # WebSocket на пути /xiaozhi/v1/, поэтому не полагаемся на путь из
        # конфига, а подставляем известный шаблон.
        if parsed.hostname and "api.tenclass.net" in parsed.hostname:
            candidate = parsed._replace(scheme=ws_scheme, path="/xiaozhi/v1/")
            derived = urlunparse(candidate)
            logger.debug(
                "Автоконвертация HTTP → WebSocket для официального Xiaozhi",
                extra={"source": self.settings.endpoint, "derived": derived},
            )
            return derived

        path = parsed.path
        if not path.endswith("/ws"):
            path = f"{path.rstrip('/')}/ws"

        candidate = parsed._replace(scheme=ws_scheme, path=path)
        derived = urlunparse(candidate)
        logger.debug(
            "Автоконвертация HTTP → WebSocket для Xiaozhi",
            extra={"source": self.settings.endpoint, "derived": derived},
        )
        return derived

    @staticmethod
    def _extract_text(payload: Dict[str, Any]) -> str:
        """Выделить осмысленный текст из произвольной структуры.

        Многие реализации возвращают поле ``text`` или вложенный объект
        ``data`` с ключами ``text``/``message``/``content``. Чтобы оставаться
        совместимыми с бесплатными облачными прокси, извлекаем строку по этим
        ключам в порядке приоритета.
        """

        direct = payload.get("text")
        if isinstance(direct, str) and direct.strip():
            return direct.strip()

        data = payload.get("data")
        if isinstance(data, dict):
            for key in ("text", "message", "content"):
                candidate = data.get(key)
                if isinstance(candidate, str) and candidate.strip():
                    return candidate.strip()

        choices = payload.get("choices")
        if isinstance(choices, list) and choices:
            message = choices[0].get("message") if isinstance(choices[0], dict) else None
            if isinstance(message, dict):
                content = message.get("content")
                if isinstance(content, str) and content.strip():
                    return content.strip()

        return ""

