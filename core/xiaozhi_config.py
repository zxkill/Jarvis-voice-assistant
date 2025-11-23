"""Управление конфигурацией интеграции с Xiaozhi."""

from __future__ import annotations

import json
import secrets
from pathlib import Path
from typing import Any, Dict

from core.logging_json import configure_logging


log = configure_logging("core.xiaozhi.config")


class XiaozhiConfigManager:
    """Простой менеджер конфигурации для хранения параметров Xiaozhi.

    Хранение вынесено в отдельный JSON‑файл, чтобы не смешивать чувствительные
    токены и идентификаторы устройства с остальными настройками ассистента.
    Менеджер отвечает за создание файла с дефолтными значениями и предоставляет
    безопасные методы для чтения/обновления отдельных полей.
    """

    def __init__(self, config_path: Path | str | None = None) -> None:
        self.config_path = Path(config_path or "config/xiaozhi.json")
        # Убеждаемся, что директория существует, иначе сохранять будет некуда.
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self._config: Dict[str, Any] = {}
        self._load()

    @property
    def data(self) -> Dict[str, Any]:
        """Возвращает текущее содержимое конфигурации.

        Возврат копии защищает от случайной модификации внутренних структур без
        последующего сохранения на диск.
        """

        return json.loads(json.dumps(self._config))

    def _load(self) -> None:
        """Читает конфигурацию с диска или создаёт файл с дефолтом.

        Любые ошибки чтения журналируются и приводят к перезаписи файла
        безопасными значениями, чтобы интеграция могла продолжить работу.
        """

        default = self._default_payload()
        if not self.config_path.exists():
            log.info("конфигурация Xiaozhi не найдена, создаю файл %s", self.config_path)
            self._config = default
            self._save()
            return
        try:
            self._config = json.loads(self.config_path.read_text(encoding="utf-8"))
            # Объединяем сохранённые данные с дефолтом, чтобы подтянуть новые поля.
            merged = {**default, **self._config}
            # Аккуратно объединяем вложенные блоки, чтобы не потерять поля mqtt/websocket
            # после обновлений схемы.
            for nested in ("network", "activation", "efuse"):
                if isinstance(default.get(nested), dict):
                    merged[nested] = {**default[nested], **(self._config.get(nested) or {})}
                    # Ещё один слой для вложенных словарей (например, mqtt, device_fingerprint).
                    for child_key, child_value in default[nested].items():
                        if isinstance(child_value, dict):
                            merged[nested][child_key] = {
                                **child_value,
                                **((self._config.get(nested) or {}).get(child_key) or {}),
                            }
            self._config = merged
        except Exception:
            log.exception("не удалось прочитать файл конфигурации Xiaozhi, восстановлю дефолт")
            self._config = default
            self._save()

    def _save(self) -> None:
        """Сериализует текущее состояние на диск."""

        try:
            payload = json.dumps(self._config, ensure_ascii=False, indent=2)
            self.config_path.write_text(payload, encoding="utf-8")
            log.debug("сохранил конфигурацию Xiaozhi", extra={"ctx": {"path": str(self.config_path)}})
        except Exception:
            log.exception("ошибка при сохранении конфигурации Xiaozhi")

    def _default_payload(self) -> Dict[str, Any]:
        """Структура по умолчанию для свежей установки."""

        # Структура приближена к оригинальному py-xiaozhi: сетевые параметры
        # и efuse‑отпечаток лежат в отдельных блоках. Это облегчает ручную
        # проверку и позволяет безболезненно копировать конфиг между машинами.
        return {
            "client_id": None,
            "device_id": None,
            "hardware_hash": None,
            # Дублируем ключи верхнего уровня для обратной совместимости с
            # ранними версиями интеграции.
            "websocket_url": None,
            "websocket_token": None,
            "ota_url": "https://api.tenclass.net/xiaozhi/ota/",
            "authorization_url": "https://xiaozhi.me/",
            # Блок сетевых настроек.
            "network": {
                "ota_url": "https://api.tenclass.net/xiaozhi/ota/",
                "websocket": {"url": None, "token": None},
                "mqtt": {
                    "endpoint": None,
                    "client_id": None,
                    "username": None,
                    "password": None,
                    "publish_topic": None,
                    "subscribe_topic": None,
                },
                # Версия протокола активации и номер приложения для заголовка
                # Activation-Version (совместимость с официальным клиентом).
                "activation_version": "v2",
                "authorization_url": "https://xiaozhi.me/",
            },
            "app_version": "2.0.0",
            # Предпочтительный язык для серверных ответов.
            "accept_language": "zh-CN",
            "activation": {
                "code": None,
                "challenge": None,
                "message": None,
                # Храним последний показанный код, чтобы не спамить чат
                # повторяющимися уведомлениями о привязке.
                "last_notified_code": None,
            },
            # Блок efuse: данные устройства, которые сервер использует для
            # привязки и проверки подлинности. Генерируется автоматически, но
            # можно перенести из оригинального клиента.
            "efuse": {
                "mac_address": None,
                "serial_number": None,
                "hmac_key": None,
                "activation_status": False,
                "device_fingerprint": {
                    "system": None,
                    "hostname": None,
                    "mac_address": None,
                    "machine_id": None,
                },
            },
        }

    def ensure_efuse(self, *, mac: str, machine_id: str, system: str, hostname: str) -> Dict[str, Any]:
        """Генерирует efuse‑отпечаток, если он отсутствует.

        Efuse нужен для полноценной имитации оригинального клиента: серверу
        достаточно MAC/серийника/ключа, чтобы принять устройство. Мы
        генерируем значения детерминированно (на базе MAC и machine_id), чтобы
        конфиг оставался стабильным между запусками.
        """

        efuse = self._config.get("efuse", {}) or {}
        # Заполняем только отсутствующие поля, чтобы не перетёреть сохранённые
        # значения пользователя.
        serial = efuse.get("serial_number") or f"SN-{machine_id[:8]}-{mac.replace(':', '')}"
        hmac_key = efuse.get("hmac_key") or secrets.token_hex(32)
        updated = {
            "mac_address": efuse.get("mac_address") or mac,
            "serial_number": serial,
            "hmac_key": hmac_key,
            "activation_status": efuse.get("activation_status", False),
            "device_fingerprint": {
                "system": efuse.get("device_fingerprint", {}).get("system") or system,
                "hostname": efuse.get("device_fingerprint", {}).get("hostname") or hostname,
                "mac_address": efuse.get("device_fingerprint", {}).get("mac_address") or mac,
                "machine_id": efuse.get("device_fingerprint", {}).get("machine_id") or machine_id,
            },
        }
        self._config["efuse"] = updated
        self._save()
        log.debug("efuse отпечаток готов", extra={"ctx": {"serial": serial, "mac": mac}})
        return updated

    def update(self, **kwargs: Any) -> Dict[str, Any]:
        """Обновляет поля конфигурации и сразу записывает изменения на диск."""

        for key, value in kwargs.items():
            if key == "activation" and isinstance(value, dict):
                current = self._config.get("activation", {})
                current.update(value)
                self._config["activation"] = current
                continue
            if key == "activation_version":
                # Сохраняем значение и в network, чтобы заголовки строились консистентно.
                net = self._config.get("network", {})
                net["activation_version"] = value
                self._config["network"] = net
                self._config[key] = value
                continue
            if key == "network" and isinstance(value, dict):
                current = self._config.get("network", {})
                # Обновляем верхний уровень и вложенные словари, чтобы не потерять mqtt/websocket.
                for nested_key, nested_value in value.items():
                    if isinstance(nested_value, dict):
                        merged_nested = {**current.get(nested_key, {}), **nested_value}
                        current[nested_key] = merged_nested
                    else:
                        current[nested_key] = nested_value
                self._config["network"] = current
                continue
            if key == "efuse" and isinstance(value, dict):
                current = self._config.get("efuse", {})
                for nested_key, nested_value in value.items():
                    if isinstance(nested_value, dict):
                        merged_nested = {**current.get(nested_key, {}), **nested_value}
                        current[nested_key] = merged_nested
                    else:
                        current[nested_key] = nested_value
                self._config["efuse"] = current
                continue
            self._config[key] = value
        self._save()
        return self.data

    def get(self, key: str, default: Any = None) -> Any:
        """Безопасное получение значения с запасным вариантом."""

        return self._config.get(key, default)

