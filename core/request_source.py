"""Контекст источника пользовательского запроса.

Модуль хранит в контекстной переменной информацию о том, откуда
поступила текущая команда: голосом или через Telegram. Это позволяет
другим компонентам (например, синтезу речи) менять поведение в
зависимости от канала.
"""

from __future__ import annotations

from contextvars import ContextVar, Token

from core.logging_json import configure_logging

# Логгер модуля для отладки изменения источника
log = configure_logging("core.request_source")

# Контекстная переменная с именем источника запроса.
# По умолчанию считаем, что команда пришла голосом.
_REQUEST_SOURCE: ContextVar[str] = ContextVar("request_source", default="voice")


def set_request_source(source: str) -> Token:
    """Установить текущий источник запроса.

    Возвращает ``Token`` для последующего восстановления предыдущего
    значения через :func:`reset_request_source`.
    """

    log.debug("set request source=%s", source)
    return _REQUEST_SOURCE.set(source)


def get_request_source() -> str:
    """Получить текущий источник запроса."""

    return _REQUEST_SOURCE.get()


def reset_request_source(token: Token) -> None:
    """Восстановить источник запроса по переданному ``token``."""

    _REQUEST_SOURCE.reset(token)
