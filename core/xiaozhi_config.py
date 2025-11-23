"""Управление конфигурацией интеграции с Xiaozhi."""

from __future__ import annotations

import json
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
            self._config = {**default, **self._config}
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

        return {
            "client_id": None,
            "device_id": None,
            "hardware_hash": None,
            "websocket_url": None,
            "websocket_token": None,
            "ota_url": "https://api.tenclass.net/xiaozhi/ota/",
            "authorization_url": "https://xiaozhi.me/",
            "activation": {
                "code": None,
                "challenge": None,
                "message": None,
            },
        }

    def update(self, **kwargs: Any) -> Dict[str, Any]:
        """Обновляет поля конфигурации и сразу записывает изменения на диск."""

        for key, value in kwargs.items():
            if key == "activation" and isinstance(value, dict):
                current = self._config.get("activation", {})
                current.update(value)
                self._config["activation"] = current
                continue
            self._config[key] = value
        self._save()
        return self.data

    def get(self, key: str, default: Any = None) -> Any:
        """Безопасное получение значения с запасным вариантом."""

        return self._config.get(key, default)

