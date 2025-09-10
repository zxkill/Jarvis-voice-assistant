from __future__ import annotations
"""Связка событийной шины с чёрной доской ``py_trees``.

Модуль подписывается на события ``core.events`` и обновляет значения
в ``Blackboard``. Это позволяет поведенческому дереву реагировать на
внешние сигналы, такие как появление пользователя в кадре или срабатывание
таймера моргания. Для каждого обработчика предусмотрено подробное
логирование в формате JSON, упрощающее анализ и отладку.
"""

import threading

from py_trees.blackboard import Blackboard

from core.events import Event, subscribe, publish
from core.logging_json import configure_logging

# Логгер модуля интеграции
log = configure_logging("behavior.integration")

# Флаг, чтобы подписка выполнялась один раз
_registered = False

def setup(start_blink_timer: bool = False, blink_interval: float = 5.0) -> None:
    """Подписать обработчики и, при необходимости, запустить таймер моргания.

    Parameters
    ----------
    start_blink_timer:
        Если ``True``, модуль дополнительно запускает внутренний таймер,
        публикующий событие ``blink.timer`` каждые ``blink_interval`` секунд.
    blink_interval:
        Интервал между публикациями события ``blink.timer``.
    """
    global _registered
    if _registered:
        return
    _registered = True

    def _on_presence_update(event: Event) -> None:
        """Обновить флаг ``face_visible`` при изменении присутствия."""
        try:
            present = bool(event.attrs.get("present"))
            Blackboard.set("face_visible", present)
            log.info(
                "получено presence.update", extra={"attrs": {"face_visible": present}}
            )
        except Exception:
            log.exception("ошибка обработки presence.update")

    def _on_blink_timer(event: Event) -> None:
        """Отметить необходимость моргания."""
        try:
            Blackboard.set("should_blink", True)
            log.info(
                "сработал таймер моргания", extra={"attrs": {"should_blink": True}}
            )
        except Exception:
            log.exception("ошибка обработки blink.timer")

    subscribe("presence.update", _on_presence_update)
    subscribe("blink.timer", _on_blink_timer)
    log.debug("обработчики событий зарегистрированы")

    if start_blink_timer:
        _start_blink_timer(blink_interval)

def _start_blink_timer(interval: float) -> None:
    """Запустить периодический таймер публикации ``blink.timer``."""

    def _publish() -> None:
        try:
            publish(Event("blink.timer"))
            log.debug("публикация blink.timer", extra={"attrs": {"interval": interval}})
        finally:
            timer = threading.Timer(interval, _publish)
            timer.daemon = True
            timer.start()

    timer = threading.Timer(interval, _publish)
    timer.daemon = True
    timer.start()
