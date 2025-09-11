"""Тесты для модуля структурированного логирования.

Проверяет, что при использовании ``log.exception`` в JSON появляется поле
``exc`` со стеком вызовов. Это упрощает отладку в продакшене.
"""

from __future__ import annotations

import io
import json
import logging

from core.logging_json import JsonFormatter, configure_logging


def test_exception_field_present() -> None:
    """При логировании исключения стек должен попадать в поле ``exc``."""
    stream = io.StringIO()
    logger = configure_logging("test.logger")
    # Перенаправляем вывод хендлера в буфер, чтобы проанализировать JSON.
    handler = logger.handlers[0]
    handler.stream = stream
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        logger.exception("fail", extra={"attrs": {"foo": "bar"}})
    data = json.loads(stream.getvalue())
    assert data["level"] == "ERROR"
    assert "RuntimeError: boom" in data.get("exc", "")


def test_timestamp_respects_timezone(monkeypatch) -> None:
    """Проверяем, что метка времени учитывает часовой пояс из ``TZ``."""
    stream = io.StringIO()
    # Устанавливаем смещение +5 часов и настраиваем логгер.
    monkeypatch.setenv("TZ", "+05:00")
    logger = configure_logging("tz.logger")
    handler = logger.handlers[0]
    handler.stream = stream
    logger.info("ping")
    data = json.loads(stream.getvalue())
    # ``isoformat`` добавляет смещение в конце строки.
    assert data["ts"].endswith("+05:00")
