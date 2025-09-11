"""Тесты для модуля структурированного логирования.

Проверяет, что при использовании ``log.exception`` в JSON появляется поле
``exc`` со стеком вызовов. Это упрощает отладку в продакшене.
"""

from __future__ import annotations

import io
import json

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


def test_logging_written_to_file(tmp_path, monkeypatch) -> None:
    """Логгер должен записывать события в указанный файл."""
    log_file = tmp_path / "jarvis.log"
    monkeypatch.setenv("LOG_FILE", str(log_file))
    logger = configure_logging("file.logger")
    logger.info("file")
    for handler in logger.handlers:
        handler.flush()
    lines = log_file.read_text(encoding="utf-8").splitlines()
    data = json.loads(lines[-1])
    assert data["message"] == "file"


def test_log_file_grows_without_rotation(tmp_path, monkeypatch) -> None:
    """Файл логов должен накапливать записи без ротации."""
    log_file = tmp_path / "jarvis.log"
    monkeypatch.setenv("LOG_FILE", str(log_file))
    logger = configure_logging("grow.logger")
    # Записываем две строки в лог, чтобы проверить отсутствие ротации.
    logger.info("first")
    logger.info("second")
    for handler in logger.handlers:
        handler.flush()
    lines = log_file.read_text(encoding="utf-8").splitlines()
    # Пропускаем первую строку с сообщением об инициализации логирования.
    messages = [json.loads(line)["message"] for line in lines[-2:]]
    assert messages == ["first", "second"]
