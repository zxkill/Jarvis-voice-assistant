"""Клиент для обращения к облачному агенту Xiaozhi.

Модуль изолирует сетевое общение с сервисом, чтобы легко переключать
бэкенды LLM через конфигурацию. Запросы и ответы подробно логируются,
в коде добавлены русские комментарии для быстрой поддержки.
"""

from __future__ import annotations

import json
import logging
import socket
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

    def __init__(self, settings: XiaozhiSettings) -> None:
        self.settings = settings
        # Чтобы не опрашивать manager-api перед каждым запросом, запоминаем,
        # что проверка привязки уже выполнялась в рамках жизни объекта.
        self._binding_checked = False
        # Клиент/устройство часто совпадают, поэтому задаём падение до device_id
        # чтобы заголовок Client-Id всегда присутствовал, как в прошивке ESP32.
        if not self.settings.client_id:
            self.settings.client_id = self.settings.device_id

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
        """Сформировать hello для WebSocket по образцу прошивки ESP32."""

        return json.dumps(
            {
                "type": "hello",
                "version": 1,
                "features": {"mcp": True},
                "transport": "websocket",
                "audio_params": {
                    "format": "opus",
                    "sample_rate": 16000,
                    "channels": 1,
                    "frame_duration": 60,
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

    def _ask_websocket_audio(
        self, wav_bytes: bytes, *, trace_id: str = "", override_endpoint: Optional[str] = None
    ) -> bytes:
        """Передать аудио в Xiaozhi по WebSocket и вернуть бинарный ответ."""

        headers = {
            "device-id": self.settings.device_id,
            "client-id": self.settings.client_id,
            "Protocol-Version": "1",
        }
        headers.update(self._build_auth_headers())
        if trace_id:
            headers["X-Trace-Id"] = trace_id

        endpoint = override_endpoint or self.settings.endpoint
        logger.debug(
            "Устанавливаем WebSocket для аудио Xiaozhi",
            extra={"endpoint": endpoint, "trace_id": trace_id, "bytes": len(wav_bytes)},
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

                # Основной аудио-трафик отправляем бинарным кадром, как это делает ESP32.
                ws.send(wav_bytes)

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

                merged = b"".join(chunks)
                logger.info(
                    "Получен аудио-ответ от Xiaozhi (WebSocket)", extra={"bytes": len(merged), "trace_id": trace_id}
                )
                return merged
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

