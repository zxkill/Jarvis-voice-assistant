"""Тесты для модуля структурированного логирования.

Проверяет, что при использовании ``log.exception`` в JSON появляется поле
``exc`` со стеком вызовов. Это упрощает отладку в продакшене.
"""

from __future__ import annotations

import io
import json
import logging

from core.logging_json import JsonFormatter, SafeRotatingFileHandler, configure_logging


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


def test_logging_written_to_file(tmp_path, monkeypatch) -> None:
    """Логгер должен записывать события в указанный файл."""
    log_file = tmp_path / "jarvis.log"
    monkeypatch.setenv("LOG_FILE", str(log_file))
    logger = configure_logging("file.logger")
    logger.info("file")
    for handler in logger.handlers:
        handler.flush()
    data = json.loads(log_file.read_text(encoding="utf-8"))
    assert data["message"] == "file"


def test_rotation_handles_permission_error(tmp_path, monkeypatch) -> None:
    """Ротация логов не должна падать при `PermissionError`."""
    log_file = tmp_path / "jarvis.log"
    monkeypatch.setenv("LOG_FILE", str(log_file))
    logger = configure_logging("rotate.logger")
    handler = next(
        h for h in logger.handlers if isinstance(h, SafeRotatingFileHandler)
    )
    # Пишем первую строку, чтобы файл появился на диске.
    logger.info("before")
    handler.flush()

    # Имитируем ошибку переименования файла на Windows.
    def fake_rename(src, dst):
        raise PermissionError("locked")

    monkeypatch.setattr(logging.handlers.os, "rename", fake_rename)

    # Вызов ротации не должен приводить к исключению.
    handler.doRollover()

    # После ротации логгер должен продолжать писать в новый файл.
    logger.info("after")
    handler.flush()
    data = json.loads(log_file.read_text(encoding="utf-8"))
    assert data["message"] == "after"
