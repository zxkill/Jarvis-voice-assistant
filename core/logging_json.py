"""Настройка структурированного JSON‑логирования."""

from __future__ import annotations

import json
import logging
import uuid
from contextvars import ContextVar
from datetime import datetime
import re


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

        log_entry = {
            # Метка времени события в формате ISO 8601
            "ts": datetime.utcfromtimestamp(record.created).isoformat() + "Z",
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
        # ensure_ascii=False — чтобы корректно выводить кириллицу
        return json.dumps(log_entry, ensure_ascii=False)


def configure_logging(component: str = "", level: int = logging.INFO) -> logging.Logger:
    """Настраивает root‑логгер на вывод JSON и возвращает логгер *component*."""

    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    handler.addFilter(ContextFilter())
    logger = logging.getLogger(component or __name__)
    logger.setLevel(level)
    logger.addHandler(handler)
    logger.propagate = False  # не передавать записи в родительские логгеры
    return logger


def new_trace_id() -> str:
    """Создать короткий идентификатор трассировки."""

    return uuid.uuid4().hex[:8]


__all__ = ["configure_logging", "TRACE_ID", "new_trace_id"]
