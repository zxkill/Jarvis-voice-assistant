"""Текстовый клиент Xiaozhi с активацией по образцу py-xiaozhi."""

from __future__ import annotations

import asyncio
import hmac
import json
import uuid
from hashlib import sha256
from typing import Any, Callable, Dict, Optional

import requests
import websockets
from websockets.exceptions import ConnectionClosed

from core.logging_json import configure_logging
from core.xiaozhi_config import XiaozhiConfigManager
from core.xiaozhi_device import XiaozhiDeviceInfo, normalize_mac


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
        # Отдельная задача для активации: запускается при получении challenge,
        # чтобы автоматически завершить привязку после ввода кода на портале.
        self._activation_task: asyncio.Task[bool] | None = None
        # Память для текстового уведомления о привязке устройства, которое надо
        # отдать пользователю один раз и не спамить в чат при каждом запросе.
        self._activation_prompt: str | None = None

    def _resolve_mac(self, fallback: Optional[str] = None) -> str:
        """Пытается получить и нормализовать MAC из разных источников.

        Порядок приоритета:
        1. efuse.mac_address, если он уже сохранён пользователем.
        2. device_id, если оно похоже на MAC (для обратной совместимости).
        3. Переданное значение fallback (обычно из DeviceInfo.profile).
        При любой ошибке выбрасываем понятное исключение, чтобы пользователь
        смог исправить конфигурацию и исключить ответ "Invalid MAC address".
        """

        candidates = [
            ("efuse", (self.config.get("efuse") or {}).get("mac_address")),
            ("device_id", self.config.get("device_id")),
            ("fallback", fallback),
        ]

        last_error: Exception | None = None
        for source, candidate in candidates:
            if candidate is None:
                continue
            try:
                normalized = normalize_mac(str(candidate))
                if source != "fallback":
                    log.debug(
                        "использую сохранённый MAC", extra={"ctx": {"source": source, "mac": normalized}}
                    )
                return normalized
            except ValueError as err:
                last_error = err
                log.warning(
                    "невалидный MAC обнаружен", extra={"ctx": {"source": source, "mac": candidate, "error": str(err)}}
                )

        # Если все варианты оказались некорректными — явно сообщаем о проблеме.
        hint = "укажите корректный MAC в config/xiaozhi.json -> efuse.mac_address"
        raise RuntimeError(f"не удалось определить MAC для Xiaozhi: {last_error or 'нет кандидатов'}; {hint}")

    def _capture_activation_prompt(self, activation: Dict[str, Any]) -> None:
        """Формирует текст подсказки для привязки устройства, если есть код.

        Метод вызывается после успешного запроса OTA. Чтобы не заспамить чат,
        мы сверяем код с ранее отправленным (`activation.last_notified_code`)
        и сохраняем сформированную подсказку во временное поле `_activation_prompt`.
        """

        code = activation.get("code")
        if not code:
            # Если код не пришёл, сбрасываем подсказку — возможно устройство уже
            # активировано, и напоминание не требуется.
            self._activation_prompt = None
            return

        last_notified = (self.config.get("activation") or {}).get("last_notified_code")
        if last_notified and str(last_notified) == str(code):
            # Код уже сообщали, повторять нет смысла.
            self._activation_prompt = None
            return

        pretty_code = " ".join(list(str(code)))
        message = activation.get("message") or "Откройте https://xiaozhi.me/ и введите код для привязки."
        self._activation_prompt = f"Код привязки Xiaozhi: {pretty_code}. {message}"
        log.info(
            "получен новый код привязки", extra={"ctx": {"code": pretty_code, "hint": message}}
        )

    def _consume_activation_prompt(self) -> str | None:
        """Возвращает и сбрасывает подсказку по привязке устройства.

        Одновременно помечает код как доставленный, сохраняя его в конфигурации,
        чтобы больше не отправлять одно и то же значение в чат.
        """

        prompt = self._activation_prompt
        if not prompt:
            return None

        activation = self.config.get("activation") or {}
        code = activation.get("code")
        if code:
            self.config.update(activation={"last_notified_code": code})
            log.info(
                "отправляю пользователю код привязки", extra={"ctx": {"code": code}}
            )

        self._activation_prompt = None
        return prompt

    async def ensure_remote_config(self) -> Dict[str, Any]:
        """Получает настройки с OTA и фиксирует их в конфиге.

        Возвращаем словарь с полями, полезными для дальнейшего подключения.
        Если сохранённые значения уже присутствуют, повторный запрос не нужен,
        но только после успешной активации устройства. До подтверждения кода
        мы принудительно обновляем OTA, чтобы поймать свежий токен/URL.
        """

        network_cfg = self.config.get("network") or {}
        activation_block = self.config.get("activation") or {}
        efuse_block = self.config.get("efuse") or {}
        cached_url = (network_cfg.get("websocket") or {}).get("url") or self.config.get("websocket_url")
        cached_token = (network_cfg.get("websocket") or {}).get("token") or self.config.get("websocket_token")

        # Сервер выдаёт временный токен/URL пока устройство не подтверждено.
        # После ввода кода привязки нужно повторно сходить в OTA, иначе
        # WebSocket может игнорировать команды. Поэтому кэш используем только
        # если активация подтверждена на стороне efuse (activation_status=True).
        activation_confirmed = bool(efuse_block.get("activation_status"))
        activation_pending = bool(activation_block.get("code")) and not activation_confirmed

        if cached_url and cached_token and activation_confirmed:
            # Даже при наличии кэша пробуем показать код активации, если он
            # был сохранён ранее, но ещё не отправлялся пользователю.
            self._capture_activation_prompt(activation_block)
            log.debug(
                "websocket конфигурация найдена в кэше", extra={"ctx": {"activation_confirmed": activation_confirmed}}
            )
            return self.config.data

        if cached_url and cached_token and activation_pending:
            log.info(
                "активация ещё не подтверждена на стороне сервера, принудительно обновляю OTA",
                extra={"ctx": {"cached_url": cached_url}},
            )

        elif cached_url and cached_token:
            # Ситуация, когда активация явно не подтверждена и код не известен.
            # Возможно, пользователь перенёс готовый конфиг вручную. Доверяем
            # этим данным, но оставляем подробное логирование для диагностики.
            self._capture_activation_prompt(activation_block)
            log.info(
                "использую сохранённые WebSocket параметры без повторной OTA",
                extra={"ctx": {"activation_confirmed": activation_confirmed}},
            )
            return self.config.data

        profile = self.device_info.profile()
        # Нормализуем MAC заранее, чтобы server-side проверка не отвергла запрос.
        mac = self._resolve_mac(profile.mac_address)
        efuse_block = self.config.ensure_efuse(
            mac=mac,
            machine_id=profile.machine_id,
            system=profile.system,
            hostname=profile.hostname,
        )
        activation_version = self.config.get("activation_version") or network_cfg.get("activation_version") or "v2"
        app_version = self.config.get("app_version") or "2.0.0"
        board_payload = self.device_info.as_payload()
        # Перезаписываем MAC внутри board_payload на нормализованный, чтобы не
        # было расхождений между efuse и полем board.mac.
        board_payload["mac"] = mac
        # Сервер py-xiaozhi ожидает, что в поле elf_sha256 прилетит hmac_key,
        # сохранённый в efuse. Это ключевой идентификатор устройства, поэтому
        # подставляем его, а не аппаратный хэш.
        elf_sha = efuse_block.get("hmac_key") or profile.hardware_hash

        payload = {
            # Блок ``application`` строго повторяет структуру py-xiaozhi:
            # версия приложения уходит в серверные логи и помогает
            # сопоставить клиентскую сборку с используемым протоколом.
            "application": {
                "version": app_version,
                "elf_sha256": elf_sha,
            },
            # Описание платы: тип и имя можно оставить как есть, сервер
            # опирается на MAC/серийник, поэтому важно передать отпечаток
            # устройства из ``DeviceInfo``.
            "board": {
                "type": "linux",
                "name": "jarvis",
                **board_payload,
            },
            # Efuse‑секция соответствует формату оригинального клиента и
            # содержит критичные идентификаторы устройства.
            "efuse": {
                "mac_address": efuse_block.get("mac_address"),
                "serial_number": efuse_block.get("serial_number"),
                "hmac_key": efuse_block.get("hmac_key"),
                "activation_status": efuse_block.get("activation_status"),
                "device_fingerprint": efuse_block.get("device_fingerprint"),
            },
        }
        headers = {
            "Device-Id": self._ensure_device_id(mac),
            "Client-Id": self._ensure_client_id(),
            "Content-Type": "application/json",
            # User-Agent повторяет оригинальный клиент: <board>/<name>-<version>.
            "User-Agent": f"linux/jarvis-{app_version}",
            # Язык ответов на стороне сервера: по умолчанию zh-CN для
            # совместимости с XiaoZhi, но поле настраиваемое в конфиге.
            "Accept-Language": self.config.get("accept_language") or "zh-CN",
        }
        # Заголовок Activation-Version нужен только для протокола v2 — его
        # значение должно совпадать с версией приложения, иначе сервер
        # вернёт 400. Храним флаг в конфиге, чтобы можно было форсировать v1.
        if activation_version.lower() == "v2":
            # Оригинальный клиент отправляет версию приложения в этом заголовке,
            # поэтому подставляем app_version, иначе сервер отвечает 400.
            headers["Activation-Version"] = app_version

        ota_url = network_cfg.get("ota_url") or self.config.get("ota_url")
        log.info("запрашиваю OTA конфигурацию Xiaozhi", extra={"ctx": {"url": ota_url}})
        try:
            response = await asyncio.to_thread(
                requests.post,
                ota_url,
                headers=headers,
                json=payload,
                timeout=10,
                verify=False,
            )
        except Exception:
            log.exception("сетевой сбой при получении OTA")
            raise

        if response.status_code != 200:
            log.error(
                "OTA вернула ошибку",
                extra={"ctx": {"status": response.status_code, "body": response.text, "headers": headers}},
            )
            # Подробное сообщение помогает понять, что именно не понравилось
            # серверу (например, неверная Activation-Version).
            raise RuntimeError(f"OTA status {response.status_code}: {response.text}")

        data = response.json()
        activation = data.get("activation") or {}
        # Сервер может вернуть новое состояние активации ("activation_status"),
        # поэтому фиксируем его в efuse, чтобы в следующий раз можно было
        # безопасно использовать кэш без дополнительного запроса OTA.
        activation_status = (
            activation.get("activation_status")
            or (data.get("efuse") or {}).get("activation_status")
            or (data.get("efuse") or {}).get("activationStatus")
            or data.get("activation_status")
        )
        # В некоторых ответах, уже после привязки устройства, сервер перестаёт
        # присылать явный activation_status и код. Если ранее код был, но теперь
        # пропал, считаем активацию подтверждённой, чтобы не застрять в режиме
        # «ожидания привязки» и сразу использовать финальный токен.
        if activation_status is None and activation_block.get("code") and not activation.get("code"):
            activation_status = True
            log.info(
                "код ввели на портале: помечаю устройство активированным",
                extra={"ctx": {"previous_code": activation_block.get("code")}},
            )
        if activation_status is not None:
            efuse_block = {**efuse_block, "activation_status": bool(activation_status)}
            self.config.update(efuse=efuse_block)
        websocket_info = data.get("websocket") or {}
        mqtt_info = data.get("mqtt") or data.get("MQTT_INFO") or {}
        updated = self.config.update(
            websocket_url=websocket_info.get("url"),
            websocket_token=websocket_info.get("token"),
            network={
                "ota_url": ota_url,
                "websocket": {"url": websocket_info.get("url"), "token": websocket_info.get("token")},
                "mqtt": mqtt_info,
                "activation_version": activation_version,
                "authorization_url": network_cfg.get("authorization_url") or self.config.get("authorization_url"),
            },
            activation={
                "code": activation.get("code"),
                "challenge": activation.get("challenge"),
                "message": activation.get("message"),
            },
            device_id=self._ensure_device_id(mac),
            client_id=self._ensure_client_id(),
            hardware_hash=profile.hardware_hash,
            efuse=efuse_block,
        )
        # Запоминаем код активации, если он пришёл, чтобы потом показать его
        # пользователю в чате и не заставлять искать сообщение в логах.
        self._capture_activation_prompt(activation)
        # Если есть challenge и устройство ещё не активировано, запускаем
        # фоновую процедуру подтверждения (POST /activate) — так py-xiaozhi
        # завершает привязку после ввода кода на портале.
        await self._start_activation_if_needed(
            activation=activation,
            efuse=efuse_block,
            ota_url=ota_url,
            device_id=self._ensure_device_id(mac),
            client_id=self._ensure_client_id(),
        )
        if not activation.get("code"):
            log.info("сервер не вернул код активации, устройство вероятно уже привязано")
        log.debug(
            "получены сетевые настройки Xiaozhi",
            extra={
                "ctx": {
                    "websocket_url": websocket_info.get("url"),
                    "mqtt_endpoint": mqtt_info.get("endpoint"),
                    "has_token": bool(websocket_info.get("token")),
                }
            },
        )
        return updated

    def _invalidate_cached_websocket(self, *, reason: str, code: int | None = None) -> None:
        """Сбрасывает сохранённые параметры WebSocket при обрыве соединения.

        Это помогает автоматически перезапросить OTA при следующей попытке и
        получить свежий токен, если предыдущий оказался протухшим или сервер
        отклонил подключение. Логирование оставляем подробным, чтобы понять,
        почему связь оборвалась на реальном устройстве.
        """

        self.config.update(
            websocket_url=None,
            websocket_token=None,
            network={"websocket": {"url": None, "token": None}},
        )
        self._ws = None
        log.warning(
            "сбросил кэш WebSocket после обрыва",
            extra={"ctx": {"reason": reason, "close_code": code}},
        )

    def _ensure_client_id(self) -> str:
        """Генерирует и сохраняет клиентский идентификатор при отсутствии."""

        client_id = self.config.get("client_id")
        if client_id:
            return str(client_id)
        client_id = str(uuid.uuid4())
        self.config.update(client_id=client_id)
        log.info("создан новый client_id для Xiaozhi", extra={"ctx": {"client_id": client_id}})
        return client_id

    def _ensure_device_id(self, mac_override: Optional[str] = None) -> str:
        """Возвращает детерминированный device_id на базе MAC."""

        device_id = self.config.get("device_id")
        if device_id:
            try:
                normalized = normalize_mac(str(device_id))
                if normalized != device_id:
                    # Приводим legacy значение к каноничному виду и сохраняем.
                    self.config.update(device_id=normalized)
                    log.info("нормализовал сохранённый device_id", extra={"ctx": {"device_id": normalized}})
                return normalized
            except ValueError:
                log.warning(
                    "device_id в конфиге некорректен, переопределяю из MAC",
                    extra={"ctx": {"device_id": device_id}},
                )

        # Используем MAC в качестве device_id для полной совместимости с
        # оригинальным клиентом Xiaozhi.
        # Используем переданный MAC или, если его нет, берём актуальный профиль
        # устройства, чтобы всегда иметь валидное значение для заголовков.
        fallback_mac = mac_override or self._resolve_mac(self.device_info.profile().mac_address)
        self.config.update(device_id=fallback_mac)
        log.info("задал новый device_id", extra={"ctx": {"device_id": fallback_mac}})
        return fallback_mac

    async def _start_activation_if_needed(
        self,
        *,
        activation: Dict[str, Any],
        efuse: Dict[str, Any],
        ota_url: str,
        device_id: str,
        client_id: str,
    ) -> None:
        """Запускает асинхронный цикл активации, если сервер прислал challenge.

        Процедура полностью повторяет логику py-xiaozhi: вычисляем HMAC по
        challenge и efuse.hmac_key, затем регулярно стучимся на /activate до
        тех пор, пока сервер не вернёт 200. Это необходимо, иначе после ввода
        кода на портале WebSocket не отдаёт ответы.
        """

        # Если устройство уже активировано — ничего делать не нужно.
        if efuse.get("activation_status"):
            return
        challenge = activation.get("challenge")
        code = activation.get("code")
        serial = efuse.get("serial_number")
        hmac_key = efuse.get("hmac_key")

        if not (challenge and serial and hmac_key and ota_url):
            return

        if self._activation_task and not self._activation_task.done():
            return

        # Запускаем долгоживущий таск — он сам запишет результат в конфиг.
        self._activation_task = asyncio.create_task(
            self._activate_device(
                activate_url=f"{ota_url.rstrip('/')}/activate",
                challenge=str(challenge),
                serial_number=str(serial),
                hmac_key=str(hmac_key),
                device_id=device_id,
                client_id=client_id,
                code=str(code) if code else None,
            )
        )

    async def _activate_device(
        self,
        *,
        activate_url: str,
        challenge: str,
        serial_number: str,
        hmac_key: str,
        device_id: str,
        client_id: str,
        code: str | None,
        max_attempts: int = 60,
        retry_interval: float = 5.0,
    ) -> bool:
        """Отправляет HMAC-подписанный challenge на эндпоинт активации."""

        try:
            key_bytes = bytes.fromhex(hmac_key)
        except ValueError:
            log.error(
                "hmac_key невалиден: ожидается hex-строка", extra={"ctx": {"hmac_key": hmac_key}}
            )
            return False

        signature = hmac.new(key_bytes, challenge.encode("utf-8"), sha256).hexdigest()
        payload = {
            "Payload": {
                "algorithm": "hmac-sha256",
                "serial_number": serial_number,
                "challenge": challenge,
                "hmac": signature,
            }
        }
        headers = {
            # Официальный клиент отправляет число 2, а не версию приложения.
            "Activation-Version": "2",
            "Device-Id": device_id,
            "Client-Id": client_id,
            "Content-Type": "application/json",
        }

        for attempt in range(max_attempts):
            log.info(
                "пытаюсь завершить активацию Xiaozhi",
                extra={"ctx": {"attempt": attempt + 1, "max_attempts": max_attempts}},
            )
            try:
                response = await asyncio.to_thread(
                    requests.post,
                    activate_url,
                    headers=headers,
                    json=payload,
                    timeout=10,
                    verify=False,
                )
            except Exception as err:
                log.warning(
                    "сбой при запросе активации, повторю позже",
                    extra={"ctx": {"error": str(err)}},
                )
                await asyncio.sleep(retry_interval)
                continue

            if response.status_code == 200:
                log.info("устройство успешно активировано на сервере Xiaozhi")
                efuse = self.config.get("efuse") or {}
                self.config.update(
                    efuse={**efuse, "activation_status": True},
                    activation={"code": None, "challenge": None},
                )
                # Сбрасываем WebSocket токен, чтобы получить свежий после активации.
                self._invalidate_cached_websocket(reason="activation complete")
                return True

            if response.status_code == 202:
                log.info("сервер ждёт ввода кода, продолжаю опрос", extra={"ctx": {"code": code}})
                await asyncio.sleep(retry_interval)
                continue

            log.warning(
                "сервер вернул ошибку при активации",
                extra={
                    "ctx": {
                        "status": response.status_code,
                        "body": response.text,
                        "attempt": attempt + 1,
                    }
                },
            )
            await asyncio.sleep(retry_interval)

        log.error("не удалось активировать устройство: превышен лимит попыток")
        return False

    async def _connect(self, *, ensure_config: bool = True) -> websockets.WebSocketClientProtocol:
        """Открывает WebSocket‑соединение, если его ещё нет.

        Параметр ``ensure_config`` позволяет пропускать повторный запрос OTA,
        если он уже выполнен ранее (например, в ``ask_text`` для получения
        кода привязки до установления соединения).
        """

        if self._ws and not self._ws.closed:
            return self._ws

        if ensure_config:
            await self.ensure_remote_config()
        network_cfg = self.config.get("network") or {}
        url = (network_cfg.get("websocket") or {}).get("url") or self.config.get("websocket_url")
        token = (network_cfg.get("websocket") or {}).get("token") or self.config.get("websocket_token")
        if not url or not token:
            raise RuntimeError("WebSocket параметры Xiaozhi не заданы")

        headers = {
            "Authorization": f"Bearer {token}",
            "Protocol-Version": "1",
            "Device-Id": self._ensure_device_id(),
            "Client-Id": self._ensure_client_id(),
        }
        log.info("открываю WebSocket с Xiaozhi", extra={"ctx": {"url": url}})
        try:
            self._ws = await websockets.connect(
                url, extra_headers=headers, ping_interval=20, ping_timeout=20
            )
        except Exception as err:
            # При ошибке подключения сразу обнуляем кэш, чтобы следующая
            # попытка запросила свежие параметры OTA.
            self._invalidate_cached_websocket(reason=str(err))
            raise

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
        """Отправляет текст на сервер Xiaozhi и возвращает первый текстовый ответ.

        В протоколе py-xiaozhi текстовая команда передаётся как событие
        ``listen/detect`` (см. ``Protocol.send_wake_word_detected`` в оригинальном
        проекте). Простое сообщение ``{"type": "text"}`` сервер игнорирует, поэтому
        формируем точный аналог wake-word события: это ключевой момент, из-за
        которого ответы не приходили после успешной активации.
        """

        async with self._lock:
            # Сначала гарантируем актуальную OTA‑конфигурацию и проверяем, нет
            # ли свежего кода активации, который нужно отдать пользователю.
            await self.ensure_remote_config()
            activation_prompt = self._consume_activation_prompt()
            if activation_prompt:
                return activation_prompt

            ws = await self._connect(ensure_config=False)
            # Сообщение повторяет py-xiaozhi: wake-word событие listen/detect с
            # полем text. Используем тот же session_id, чтобы сервер связал
            # диалог с последующими аудио/текстовыми ответами.
            payload = {
                "type": "listen",
                "state": "detect",
                "text": text,
                "session_id": self._session_id,
            }
            if trace_id:
                payload["trace_id"] = trace_id
            log.info(
                "отправляю текст в Xiaozhi",
                extra={"ctx": {"text": text, "trace_id": trace_id, "session_id": self._session_id}},
            )
            await ws.send(json.dumps(payload))
            try:
                return await asyncio.wait_for(self._wait_text_reply(ws), timeout=timeout)
            except asyncio.TimeoutError:
                log.warning("не дождался ответа Xiaozhi вовремя")
                return None

    async def _wait_text_reply(self, ws: websockets.WebSocketClientProtocol) -> str | None:
        """Ждёт первый текстовый ответ и возвращает его контент."""

        try:
            async for message in ws:
                if isinstance(message, bytes):
                    log.debug("получены бинарные данные от Xiaozhi, пропускаю")
                    continue
                try:
                    data = json.loads(message)
                except json.JSONDecodeError:
                    log.debug("получил не‑JSON, вернул как есть")
                    return message

                log.debug("получено сообщение от Xiaozhi", extra={"ctx": data})
                text_fields = [
                    data.get("text"),
                    data.get("response"),
                    (data.get("message") or {}).get("text") if isinstance(data.get("message"), dict) else None,
                ]
                for entry in text_fields:
                    if entry:
                        log.info("получен ответ Xiaozhi", extra={"ctx": {"text": entry}})
                        # Раз ответ пришёл, можно считать устройство успешно
                        # активированным: фиксируем флаг, чтобы больше не дёргать OTA.
                        efuse = self.config.get("efuse") or {}
                        if not efuse.get("activation_status"):
                            self.config.update(efuse={**efuse, "activation_status": True})
                        return entry
                log.debug("получено промежуточное сообщение без текста", extra={"ctx": data})
        except ConnectionClosed as err:
            # Если соединение закрыли без текста, сбрасываем токен, чтобы
            # следующая попытка запросила новую конфигурацию OTA.
            try:
                reason_text = str(err)
            except Exception:
                # На всякий случай защищаемся от нестандартных объектов err,
                # у которых __str__ может падать.
                reason_text = getattr(err, "reason", "WebSocket closed")
            self._invalidate_cached_websocket(reason=reason_text, code=err.code)
            log.error(
                "WebSocket закрыт до получения ответа", extra={"ctx": {"code": err.code, "reason": err.reason}}
            )
            return None
        return None


def build_client(factory: Callable[[], XiaozhiClient] | None = None) -> XiaozhiClient:
    """Создаёт клиент с возможностью замены фабрики для тестов."""

    return (factory or XiaozhiClient)()

