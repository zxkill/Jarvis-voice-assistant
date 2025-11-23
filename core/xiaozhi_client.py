"""Текстовый клиент Xiaozhi с активацией по образцу py-xiaozhi."""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any, Callable, Dict

import requests
import websockets

from core.logging_json import configure_logging
from core.xiaozhi_config import XiaozhiConfigManager
from core.xiaozhi_device import XiaozhiDeviceInfo


log = configure_logging("core.xiaozhi.client")


class XiaozhiClient:
    """Минимальный клиент для текстового обмена с сервером Xiaozhi.

    Класс повторяет критичные части py-xiaozhi: сбор отпечатка устройства,
    запрос конфигурации OTA (WebSocket URL/токен + код активации) и сам диалог
    через WebSocket. Реализация деликатно работает с конфигурацией и избегает
    блокировки event loop — все сетевые вызовы вынесены в отдельные потоки.
    """

    def __init__(self, config_manager: XiaozhiConfigManager | None = None) -> None:
        self.config = config_manager or XiaozhiConfigManager()
        self.device_info = XiaozhiDeviceInfo()
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._lock = asyncio.Lock()
        self._session_id = str(uuid.uuid4())

    async def ensure_remote_config(self) -> Dict[str, Any]:
        """Получает настройки с OTA и фиксирует их в конфиге.

        Возвращаем словарь с полями, полезными для дальнейшего подключения.
        Если сохранённые значения уже присутствуют, повторный запрос не нужен.
        """

        cached_url = self.config.get("websocket_url")
        cached_token = self.config.get("websocket_token")
        if cached_url and cached_token:
            log.debug("websocket конфигурация найдена в кэше")
            return self.config.data

        payload = {
            "application": {
                "version": "jarvis-integration",
                "elf_sha256": self.device_info.profile().hardware_hash,
            },
            "board": {
                "type": "linux",
                "name": "jarvis",
                **self.device_info.as_payload(),
            },
        }
        headers = {
            "Device-Id": self._ensure_device_id(),
            "Client-Id": self._ensure_client_id(),
            "Content-Type": "application/json",
            "User-Agent": "jarvis-xiaozhi",
            "Accept-Language": "ru-RU",
            "Activation-Version": "2",
        }

        log.info("запрашиваю OTA конфигурацию Xiaozhi", extra={"ctx": {"url": self.config.get("ota_url")}})
        try:
            response = await asyncio.to_thread(
                requests.post,
                self.config.get("ota_url"),
                headers=headers,
                json=payload,
                timeout=10,
                verify=False,
            )
        except Exception:
            log.exception("сетевой сбой при получении OTA")
            raise

        if response.status_code != 200:
            log.error("OTA вернула ошибку", extra={"ctx": {"status": response.status_code, "body": response.text}})
            raise RuntimeError(f"OTA status {response.status_code}")

        data = response.json()
        activation = data.get("activation") or {}
        websocket_info = data.get("websocket") or {}
        updated = self.config.update(
            websocket_url=websocket_info.get("url"),
            websocket_token=websocket_info.get("token"),
            activation={
                "code": activation.get("code"),
                "challenge": activation.get("challenge"),
                "message": activation.get("message"),
            },
            device_id=self._ensure_device_id(),
            client_id=self._ensure_client_id(),
            hardware_hash=self.device_info.profile().hardware_hash,
        )
        if activation.get("code"):
            pretty_code = " ".join(list(str(activation.get("code"))))
            log.info(
                "получен код привязки устройства", extra={"ctx": {"code": pretty_code, "hint": activation.get("message")}}
            )
        else:
            log.info("сервер не вернул код активации, устройство вероятно уже привязано")
        return updated

    def _ensure_client_id(self) -> str:
        """Генерирует и сохраняет клиентский идентификатор при отсутствии."""

        client_id = self.config.get("client_id")
        if client_id:
            return str(client_id)
        client_id = str(uuid.uuid4())
        self.config.update(client_id=client_id)
        log.info("создан новый client_id для Xiaozhi", extra={"ctx": {"client_id": client_id}})
        return client_id

    def _ensure_device_id(self) -> str:
        """Возвращает детерминированный device_id на базе hardware_hash."""

        device_id = self.config.get("device_id")
        if device_id:
            return str(device_id)
        device_id = self.device_info.profile().hardware_hash[:16]
        self.config.update(device_id=device_id)
        log.info("задал новый device_id", extra={"ctx": {"device_id": device_id}})
        return device_id

    async def _connect(self) -> websockets.WebSocketClientProtocol:
        """Открывает WebSocket‑соединение, если его ещё нет."""

        if self._ws and not self._ws.closed:
            return self._ws

        await self.ensure_remote_config()
        url = self.config.get("websocket_url")
        token = self.config.get("websocket_token")
        if not url or not token:
            raise RuntimeError("WebSocket параметры Xiaozhi не заданы")

        headers = {
            "Authorization": f"Bearer {token}",
            "Protocol-Version": "1",
            "Device-Id": self._ensure_device_id(),
            "Client-Id": self._ensure_client_id(),
        }
        log.info("открываю WebSocket с Xiaozhi", extra={"ctx": {"url": url}})
        self._ws = await websockets.connect(url, extra_headers=headers, ping_interval=20, ping_timeout=20)
        await self._send_hello()
        return self._ws

    async def _send_hello(self) -> None:
        """Отправляет приветственное сообщение, как делает оригинальный клиент."""

        if not self._ws:
            return
        hello_message = {
            "type": "hello",
            "version": 1,
            "features": {"mcp": True},
            "transport": "websocket",
        }
        await self._ws.send(json.dumps(hello_message))

    async def ask_text(self, text: str, trace_id: str | None = None, timeout: float = 20.0) -> str | None:
        """Отправляет текст на сервер Xiaozhi и возвращает первый текстовый ответ."""

        async with self._lock:
            ws = await self._connect()
            payload = {"type": "text", "text": text, "session_id": self._session_id}
            if trace_id:
                payload["trace_id"] = trace_id
            log.info("отправляю текст в Xiaozhi", extra={"ctx": {"text": text, "trace_id": trace_id}})
            await ws.send(json.dumps(payload))
            try:
                return await asyncio.wait_for(self._wait_text_reply(ws), timeout=timeout)
            except asyncio.TimeoutError:
                log.warning("не дождался ответа Xiaozhi вовремя")
                return None

    async def _wait_text_reply(self, ws: websockets.WebSocketClientProtocol) -> str | None:
        """Ждёт первый текстовый ответ и возвращает его контент."""

        async for message in ws:
            if isinstance(message, bytes):
                continue
            try:
                data = json.loads(message)
            except json.JSONDecodeError:
                log.debug("получил не‑JSON, вернул как есть")
                return message

            text_fields = [
                data.get("text"),
                data.get("response"),
                (data.get("message") or {}).get("text") if isinstance(data.get("message"), dict) else None,
            ]
            for entry in text_fields:
                if entry:
                    log.info("получен ответ Xiaozhi", extra={"ctx": {"text": entry}})
                    return entry
            log.debug("получено промежуточное сообщение", extra={"ctx": data})
        return None


def build_client(factory: Callable[[], XiaozhiClient] | None = None) -> XiaozhiClient:
    """Создаёт клиент с возможностью замены фабрики для тестов."""

    return (factory or XiaozhiClient)()

