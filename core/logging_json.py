"""Настройка структурированного JSON‑логирования."""

from __future__ import annotations

import json
import logging
from logging.handlers import RotatingFileHandler
import uuid
from contextvars import ContextVar
import os
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo


TRACE_ID: ContextVar[str] = ContextVar("trace_id", default="")


class ContextFilter(logging.Filter):
    """Добавляет ``trace_id`` из контекста в запись лога."""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: D401
        record.trace_id = TRACE_ID.get()
        return True


class JsonFormatter(logging.Formatter):
    """Форматирует записи логов в компактный JSON."""

    def format(self, record: logging.LogRecord) -> str:
        """Преобразует запись ``record`` в строку JSON."""

        def _mask(obj):  # рекурсивная анонимизация
            if isinstance(obj, str):
                obj = re.sub(r"[\w.+-]+@[\w-]+\.[\w.-]+", "<email>", obj)
                obj = re.sub(r"\b\d{3,}\b", "<num>", obj)
                return obj
            if isinstance(obj, dict):
                return {k: _mask(v) for k, v in obj.items()}
            if isinstance(obj, list):
                return [_mask(v) for v in obj]
            return obj

        # Определяем часовой пояс для метки времени. Он берётся из
        # переменной окружения ``TZ``. Допустимы как названия зон из
        # базы IANA (``Europe/Moscow``), так и числовые смещения вида
        # ``+05:00``. Если значение некорректно, используется ``UTC``.
        def _resolve_tz() -> timezone:
            tz_name = os.getenv("TZ", "UTC")
            if tz_name.upper() == "UTC":
                return timezone.utc
            # Поддержка формата "+HH:MM" или "-HH" для простых смещений.
            if re.fullmatch(r"[+-]\d{1,2}(:\d{2})?", tz_name):
                sign = 1 if tz_name.startswith("+") else -1
                hours, _, minutes = tz_name[1:].partition(":")
                delta = timedelta(
                    hours=int(hours or 0), minutes=int(minutes or 0)
                )
                return timezone(sign * delta)
            try:
                return ZoneInfo(tz_name)
            except Exception:
                return timezone.utc

        log_entry = {
            # Метка времени события в формате ISO 8601 с учётом часового пояса
            "ts": datetime.fromtimestamp(record.created, _resolve_tz()).isoformat(),
            # Уровень логирования (INFO, ERROR и т.д.)
            "level": record.levelname,
            # Имя компонента, либо logger.name, если не передано через extra
            "component": getattr(record, "component", record.name),
            # Название события, произвольная строка
            "event": getattr(record, "event", ""),
            # Идентификатор трассировки для связывания логов
            "trace_id": getattr(record, "trace_id", ""),
            # Дополнительные атрибуты события с анонимизацией
            "attrs": _mask(getattr(record, "attrs", {})),
            # Основное сообщение лога без персональных данных
            "message": _mask(record.getMessage()),
        }
        # Если передан traceback, добавляем его в поле ``exc``. Это помогает
        # быстро диагностировать ошибки, так как стек вызовов виден прямо в
        # JSON-логе.
        if record.exc_info:
            log_entry["exc"] = self.formatException(record.exc_info)
        # ensure_ascii=False — чтобы корректно выводить кириллицу
        return json.dumps(log_entry, ensure_ascii=False)


def configure_logging(component: str = "", level: int = logging.INFO) -> logging.Logger:
    """Настраивает root‑логгер на вывод JSON и возвращает логгер *component*."""

    # Потоковый хендлер выводит логи в stdout, чтобы их было видно в консоли.
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(JsonFormatter())
    stream_handler.addFilter(ContextFilter())

    # Определяем файл для записи логов. Путь можно переопределить
    # переменной окружения ``LOG_FILE``. По умолчанию пишем в ``logs/jarvis.log``.
    log_file = os.getenv("LOG_FILE", os.path.join("logs", "jarvis.log"))
    os.makedirs(os.path.dirname(log_file), exist_ok=True)

    # Ротация файла предотвращает переполнение диска: храним до пяти файлов
    # по ~1 МБ каждый.
    file_handler = RotatingFileHandler(
        log_file, maxBytes=1_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(JsonFormatter())
    file_handler.addFilter(ContextFilter())

    logger = logging.getLogger(component or __name__)
    logger.setLevel(level)
    logger.addHandler(stream_handler)
    logger.addHandler(file_handler)
    logger.propagate = False  # не передавать записи в родительские логгеры
    return logger


def new_trace_id() -> str:
    """Создать короткий идентификатор трассировки."""

    return uuid.uuid4().hex[:8]


__all__ = ["configure_logging", "TRACE_ID", "new_trace_id"]
