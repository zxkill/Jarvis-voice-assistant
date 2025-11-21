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
    timeout: float = 40.0


class XiaozhiClient:
    """Клиент Xiaozhi с поддержкой HTTP и WebSocket протоколов.

    По умолчанию пытаемся выбрать подходящий транспорт по схеме URL:
    ``http/https`` → обычный POST, ``ws/wss`` → WebSocket-сеанс. Это приближает
    поведение к прошивке ESP32, где основное общение с моделью идёт через
    WebSocket, но сохраняет обратную совместимость с HTTP‑прокси.
    """

    def __init__(self, settings: XiaozhiSettings) -> None:
        self.settings = settings

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

    def ask(self, prompt: str, *, trace_id: str = "") -> str:
        """Отправить запрос в Xiaozhi и вернуть текстовый ответ.

        При сетевой ошибке или пустом ответе возбуждается :class:`RuntimeError`.
        """

        if self.settings.endpoint.startswith(("ws://", "wss://")):
            return self._ask_websocket(prompt, trace_id=trace_id)

        payload = self._build_payload(prompt)
        headers = {"Content-Type": "application/json"}
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

    def _ask_websocket(self, prompt: str, *, trace_id: str = "", override_endpoint: Optional[str] = None) -> str:
        """Отправить текст в Xiaozhi через WebSocket и дождаться ответа.

        Мы повторяем базовый handshake из документации проекта: открываем сессию,
        отправляем единичное текстовое сообщение с полями ``share_code`` и
        ``input`` и читаем первые осмысленные данные в ответ. Формат ответа
        совпадает с HTTP‑веткой, поэтому повторно используем извлечение текста.
        """

        headers = {}
        if trace_id:
            headers["X-Trace-Id"] = trace_id
        if self.settings.agent_code:
            # Сайт передаёт share code в теле, но для WebSocket полезно продублировать
            # его в header, чтобы прокси мог быстро отвергать чужие подключения.
            headers["X-Share-Code"] = self.settings.agent_code

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

