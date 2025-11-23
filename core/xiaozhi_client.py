"""Текстовый клиент Xiaozhi с активацией по образцу py-xiaozhi."""

from __future__ import annotations

import asyncio
import hmac
import json
import ssl
import uuid
from hashlib import sha256
from typing import Any, Callable, Dict, Optional

import requests
import websockets
from websockets.exceptions import ConnectionClosed, InvalidStatusCode

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
        # Флаг, показывающий что сервер ответил hello и готов принимать команды
        # (py-xiaozhi ждёт этот ответ перед началом диалога).
        self._hello_confirmed = asyncio.Event()
        # Фоновый слушатель сообщений WebSocket, чтобы максимально повторить
        # протокол py-xiaozhi: он непрерывно читает кадры и уведомляет об
        # «hello», а также складывает все остальные JSON в очередь для
        # дальнейшей обработки.
        self._message_task: asyncio.Task[None] | None = None
        self._incoming_messages: asyncio.Queue[Any] = asyncio.Queue()
        self._ws_close_reason: str | None = None
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

    async def ensure_remote_config(self, *, force_refresh: bool = False) -> Dict[str, Any]:
        """Получает настройки с OTA и фиксирует их в конфиге.

        Возвращаем словарь с полями, полезными для дальнейшего подключения.
        Если сохранённые значения уже присутствуют, повторный запрос не нужен,
        но только после успешной активации устройства. До подтверждения кода
        мы принудительно обновляем OTA, чтобы поймать свежий токен/URL.

        Аргумент ``force_refresh`` позволяет принудительно переспросить OTA даже
        при наличии валидного кэша. Это нужно, если сервер разорвал WebSocket
        или токен протух — в таком случае мы полностью повторяем поведение
        py-xiaozhi и получаем новый набор параметров без перезапуска клиента.
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

        if force_refresh:
            log.info(
                "принудительно обновляю OTA, игнорируя кэш",
                extra={"ctx": {"cached_url": cached_url, "has_token": bool(cached_token)}},
            )

        if cached_url and cached_token and activation_confirmed and not force_refresh:
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

        elif cached_url and cached_token and not force_refresh:
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
        # Останавливаем фоновые задачи чтения, чтобы новая попытка подключиться
        # стартовала «с чистого листа» и не столкнулась с оставшимся генератором.
        if self._message_task and not self._message_task.done():
            self._message_task.cancel()
        self._message_task = None
        # Перед закрытием подчёркиваем причину, чтобы видеть её в логах при
        # повторном подключении.
        self._ws_close_reason = reason
        self._ws = None
        # Отправляем служебный маркер в очередь сообщений, чтобы ожидающие
        # ответы корутины могли завершиться без таймаута.
        try:
            self._incoming_messages.put_nowait(None)
        except Exception:
            # Очередь может быть неинициализирована при раннем фейле подключения.
            pass
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
            # Подробно фиксируем состояние кэша, чтобы понимать, почему мы не
            # открываем новое соединение. Это поможет отличить «живой» сокет
            # от залипшего состояния, когда очередь пуста, а hello не пришёл.
            log.debug(
                "использую уже открытый WebSocket",
                extra={
                    "ctx": {
                        "url": getattr(self._ws, "host", None),
                        "closed": getattr(self._ws, "closed", None),
                        "close_code": getattr(self._ws, "close_code", None),
                    }
                },
            )
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
        # Используем отключенную проверку сертификатов, как это делает
        # оригинальный py-xiaozhi, чтобы не упасть на кастомных CA.
        ssl_context = ssl._create_unverified_context()
        log.info(
            "открываю WebSocket с Xiaozhi",
            extra={
                "ctx": {
                    "url": url,
                    "token_tail": token[-6:] if token else None,
                    "device_id": headers.get("Device-Id"),
                    "client_id": headers.get("Client-Id"),
                }
            },
        )
        try:
            connect_kwargs = {
                "ping_interval": 20,
                "ping_timeout": 20,
                "close_timeout": 10,
                "max_size": 10 * 1024 * 1024,
                "compression": None,
                "ssl": ssl_context,
            }
            log.debug("параметры подключения WebSocket", extra={"ctx": connect_kwargs})
            try:
                # Websockets 12+ использует additional_headers
                self._ws = await websockets.connect(
                    url,
                    additional_headers=headers,
                    **connect_kwargs,
                )
            except TypeError:
                # Более старые версии принимают extra_headers
                self._ws = await websockets.connect(
                    url,
                    extra_headers=headers,
                    **connect_kwargs,
                )
        except InvalidStatusCode as err:
            # Подробно логируем коды/заголовки рукопожатия, чтобы понимать,
            # почему сервер отверг соединение (например, протухший токен).
            self._invalidate_cached_websocket(reason=f"ws status {err.status_code}")
            log.error(
                "WebSocket отклонён сервером",
                extra={
                    "ctx": {
                        "status": err.status_code,
                        "headers": dict(err.headers or {}),
                        "url": url,
                        "device_id": headers.get("Device-Id"),
                        "client_id": headers.get("Client-Id"),
                    }
                },
            )
            raise
        except Exception as err:
            # При ошибке подключения сразу обнуляем кэш, чтобы следующая
            # попытка запросила свежие параметры OTA. Добавляем расширенный
            # контекст (URL/ID), чтобы видеть, на какой стадии рвётся соединение.
            self._invalidate_cached_websocket(reason=str(err))
            log.error(
                "ошибка установления WebSocket",
                extra={
                    "ctx": {
                        "error": str(err),
                        "url": url,
                        "device_id": headers.get("Device-Id"),
                        "client_id": headers.get("Client-Id"),
                    }
                },
            )
            raise

        # Обновляем очередь и служебные события для свежего подключения.
        self._incoming_messages = asyncio.Queue()
        self._hello_confirmed = asyncio.Event()
        # Стартуем фоновую задачу чтения, чтобы не потерять server hello и
        # чтобы последующие ответы появлялись в очереди без гонок.
        self._message_task = asyncio.create_task(self._message_loop())
        log.info(
            "создал фоновую задачу чтения WebSocket",
            extra={"ctx": {"task": id(self._message_task)}},
        )

        # Сразу фиксируем детали рукопожатия в INFO, чтобы пользователь видел,
        # какой ответ дал сервер (в оригинальном клиенте помогает ловить 4xx).
        try:
            log.info(
                "рукопожатие WebSocket успешно",
                extra={
                    "ctx": {
                        "response_headers": dict(getattr(self._ws, "response_headers", {}) or {}),
                        "subprotocol": getattr(self._ws, "subprotocol", None),
                    }
                },
            )
        except Exception:
            # Заголовки могут отсутствовать у заглушек в тестах — просто пропускаем.
            pass

        await self._send_hello()
        await self._wait_for_server_hello()
        # Если фоновый слушатель успел закрыть соединение (например, сервер
        # немедленно сбросил рукопожатие), фиксируем это и вызываем ошибку, чтобы
        # верхний уровень мог повторить запрос OTA/соединения, как делает
        # py-xiaozhi при 4401/401.
        if not self._ws or getattr(self._ws, "closed", False):
            self._invalidate_cached_websocket(reason="ws closed after hello wait", code=None)
            raise RuntimeError("WebSocket закрылся до начала диалога")
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
            # Добавляем аудио‑параметры как в py-xiaozhi, чтобы сервер видел
            # привычную структуру hello и не отклонял последующие listen/detect
            # события. Даже если мы не шлём аудио, поля помогают серверу
            # корректно инициализировать сессию.
            "audio_params": {
                "format": "opus",
                "sample_rate": 16000,
                "channels": 1,
                "frame_duration": 20,
            },
        }
        log.info("отправляю hello в Xiaozhi", extra={"ctx": hello_message})
        await self._ws.send(json.dumps(hello_message))

    async def _wait_for_server_hello(self, timeout: float = 10.0) -> None:
        """Ожидает ответ hello от сервера перед отправкой команд.

        Оригинальный клиент не начинает обмен сообщениями, пока не увидит
        подтверждение hello. В бою сервер иногда может не прислать приветствие
        (наблюдается в логах пользователя), поэтому таймаут не роняет процесс,
        а лишь фиксирует проблему и даёт продолжить обмен. Так проще сравнить
        поведение с py-xiaozhi и увидеть, возвращаются ли ответы на listen/detect
        даже без server hello.
        """

        if not self._ws:
            raise RuntimeError("WebSocket не инициализирован")

        try:
            log.info(
                "жду server hello от Xiaozhi",
                extra={
                    "ctx": {
                        "timeout": timeout,
                        "url": getattr(self._ws, "host", None),
                        "has_reader": bool(self._message_task),
                    }
                },
            )
            await asyncio.wait_for(self._hello_confirmed.wait(), timeout=timeout)
            log.info(
                "сервер подтвердил hello",
                extra={"ctx": {"transport": "websocket", "url": getattr(self._ws, "host", None)}},
            )
        except asyncio.TimeoutError:
            # При таймауте достанем внутренний буфер очереди, чтобы показать,
            # приходили ли какие-либо пакеты от сервера (даже не-JSON). Это
            # помогает понять, зависает ли сервер на hello или вовсе не шлёт
            # никаких кадров (как в текущих логах пользователя).
            pending_messages = []
            try:
                raw_queue = getattr(self._incoming_messages, "_queue", [])
                pending_messages = list(raw_queue)
            except Exception:
                pending_messages = []

            log.error(
                "таймаут ожидания server hello",
                extra={
                    "ctx": {
                        "pending_queue": getattr(self._incoming_messages, "qsize", lambda: None)(),
                        "url": getattr(self._ws, "host", None),
                        "pending_preview": pending_messages[:3],
                    }
                },
            )
            # В условиях боевого сервера лучше не падать: фиксируем метрику и
            # позволяем продолжить отправку listen/detect, чтобы проверить,
            # ответит ли сервер без явного приветствия.
            self._hello_confirmed.set()
            return

    async def _prepend_message(self, message: str) -> None:
        """Возвращает сообщение обратно в поток чтения.

        В клиенте websockets нет стандартного буфера, поэтому мы переиспользуем
        простую очередь для чтения в `_wait_text_reply`: помещаем сообщение в
        начало через локальный async-генератор.
        """

        if not self._ws:
            return
            # websockets не предоставляет публичного API для возврата сообщения,
            # поэтому используем небольшой трюк: создаём таск, который немедленно
            # отправит сообщение в сторону клиента, где оно будет считано как
            # очередной кадр. Для тестов DummyWebSocket реализует feed.
            if hasattr(self._ws, "feed"):
                await self._ws.feed(message)  # type: ignore[attr-defined]
            else:
                # В бою просто логируем — сервер обычно не шлёт лишних сообщений
                # до hello, поэтому потеря одного пакета маловероятна.
                log.debug("не удалось буферизовать сообщение", extra={"ctx": {"message": message}})

    async def _message_loop(self) -> None:
        """Постоянно читает WebSocket и складывает JSON в очередь.

        Эта корутина копирует подход py-xiaozhi: отдельный обработчик сообщений
        поднимается сразу после подключения, фиксирует server hello, складывает
        полезные ответы в очередь `_incoming_messages` и реагирует на обрывы,
        сбрасывая кэш токена. Так мы исключаем гонки между hello и listen/detect
        и получаем более детальные логи по причинам закрытия соединения.
        """

        if not self._ws:
            return

        try:
            log.info(
                "запустил фоновый слушатель WebSocket",
                extra={"ctx": {"url": getattr(self._ws, "host", None)}},
            )
            async for message in self._ws:
                if isinstance(message, bytes):
                    log.debug(
                        "получен бинарный кадр от Xiaozhi", extra={"ctx": {"size": len(message)}}
                    )
                    continue

                try:
                    data = json.loads(message)
                except json.JSONDecodeError:
                    log.info("получен не‑JSON от Xiaozhi", extra={"ctx": {"message": message}})
                    await self._incoming_messages.put(message)
                    continue

                msg_type = data.get("type")
                if msg_type == "hello":
                    self._hello_confirmed.set()
                    log.info("поймал server hello в фоне", extra={"ctx": data})
                    continue

                log.info("положил входящее сообщение в очередь", extra={"ctx": data})
                await self._incoming_messages.put(data)

        except ConnectionClosed as err:
            log.warning(
                "WebSocket закрыт удалённой стороной",
                extra={"ctx": {"code": err.code, "reason": err.reason}},
            )
            self._invalidate_cached_websocket(reason=str(err), code=err.code)
        except Exception:
            log.exception("ошибка при чтении WebSocket")
            self._invalidate_cached_websocket(reason="message loop crash")
        finally:
            # Служебный маркер завершения, чтобы все ожидающие ответы сразу
            # прекратили ожидание и инициировали переподключение при необходимости.
            await self._incoming_messages.put(None)

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

            try:
                ws = await self._connect(ensure_config=False)
            except Exception as err:
                log.error(
                    "не удалось открыть WebSocket перед отправкой текста",
                    extra={"ctx": {"error": str(err)}},
                )
                # Восстанавливаем соединение по образцу py-xiaozhi: если кэш
                # токена сброшен из-за ошибки, пытаемся заново запросить OTA и
                # сразу переподключиться.
                try:
                    await self.ensure_remote_config(force_refresh=True)
                    ws = await self._connect(ensure_config=False)
                except Exception as retry_err:
                    log.error(
                        "повторное подключение к Xiaozhi не удалось",
                        extra={"ctx": {"error": str(retry_err)}},
                    )
                    return None
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
            return await self._wait_text_reply(ws, timeout=timeout)

    async def _wait_text_reply(self, ws: websockets.WebSocketClientProtocol, timeout: float = 20.0) -> str | None:
        """Ждёт первый текстовый ответ и возвращает его контент."""

        while True:
            try:
                message = await asyncio.wait_for(self._incoming_messages.get(), timeout=timeout)
            except asyncio.TimeoutError:
                log.warning("не дождался ответа Xiaozhi вовремя")
                return None

            if message is None:
                log.warning(
                    "получен сигнал о закрытии сокета до ответа",
                    extra={"ctx": {"reason": self._ws_close_reason}},
                )
                return None

            if isinstance(message, bytes):
                log.debug("получены бинарные данные от Xiaozhi, пропускаю")
                continue

            if isinstance(message, str):
                try:
                    data = json.loads(message)
                except json.JSONDecodeError:
                    log.debug("получил не‑JSON, вернул как есть")
                    return message
            else:
                data = message

            log.debug("получено сообщение от Xiaozhi", extra={"ctx": data})
            text_fields = [
                data.get("text") if isinstance(data, dict) else None,
                data.get("response") if isinstance(data, dict) else None,
                (data.get("message") or {}).get("text") if isinstance(data, dict) and isinstance(data.get("message"), dict) else None,
            ]
            for entry in text_fields:
                if entry:
                    log.info("получен ответ Xiaozhi", extra={"ctx": {"text": entry}})
                    efuse = self.config.get("efuse") or {}
                    if not efuse.get("activation_status"):
                        self.config.update(efuse={**efuse, "activation_status": True})
                    return entry
            log.debug("получено промежуточное сообщение без текста", extra={"ctx": data})


def build_client(factory: Callable[[], XiaozhiClient] | None = None) -> XiaozhiClient:
    """Создаёт клиент с возможностью замены фабрики для тестов."""

    return (factory or XiaozhiClient)()

