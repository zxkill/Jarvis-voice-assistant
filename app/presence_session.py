from __future__ import annotations
"""Управление пользовательскими сессиями на основе событий присутствия.

Модуль подписывается на события ``presence.update`` и создаёт/закрывает записи
в базе данных при появлении или исчезновении человека перед камерой.
"""

from typing import Optional

from core.events import Event, subscribe
from core.logging_json import configure_logging
from memory.writer import start_session, end_session


def setup_presence_session(owner_id: str) -> None:
    """Подписаться на обновления присутствия и управлять жизненным циклом сессии.

    Каждая активная сессия фиксируется в базе данных. При появлении пользователя
    создаётся новая запись, а при его уходе — завершает текущую.
    """
    presence_log = configure_logging("presence.session")
    session_id: Optional[int] = None

    def _on_presence(event: Event) -> None:
        """Внутренний обработчик событий ``presence.update``."""
        nonlocal session_id
        try:
            if event.attrs.get("present"):
                # Пользователь появился
                if session_id is None:
                    session_id = start_session(owner_id)
                    presence_log.info(
                        "session started", extra={"session_id": session_id}
                    )
                else:
                    # Если сессия уже активна, повторное событие считаем ошибкой
                    presence_log.error(
                        "session already active", extra={"session_id": session_id}
                    )
            else:
                # Пользователь ушёл
                if session_id is not None:
                    end_session(session_id)
                    presence_log.info(
                        "session ended", extra={"session_id": session_id}
                    )
                    session_id = None
                else:
                    presence_log.error("no active session to end")
        except Exception:
            presence_log.exception("error handling presence.update")

    subscribe("presence.update", _on_presence)
