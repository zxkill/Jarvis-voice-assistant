"""Простейший pub/sub‑шлюз для взаимодействия компонентов."""

from collections import defaultdict
from dataclasses import dataclass, field
import logging
from typing import Any, Callable, Dict, List


@dataclass
class Event:
    """Событие, передаваемое между частями системы."""

    # Тип события (например, ``user_query_started``)
    kind: str
    # Дополнительные атрибуты события
    attrs: Dict[str, Any] = field(default_factory=dict)


# Словарь, где по типу события хранится список обработчиков
_subscribers: Dict[str, List[Callable[[Event], None]]] = defaultdict(list)
# Глобальные подписчики, получающие все события
_global_subscribers: List[Callable[[Event], None]] = []


log = logging.getLogger(__name__)


def subscribe(kind: str, callback: Callable[[Event], None]) -> None:
    """Регистрирует обработчик *callback* для событий типа *kind*.

    Для подписки на все типы событий используйте :func:`subscribe_all`.
    """

    _subscribers[kind].append(callback)
    log.debug("Subscribed %s to %s", getattr(callback, "__name__", repr(callback)), kind)


def subscribe_all(callback: Callable[[Event], None]) -> None:
    """Регистрирует обработчик *callback* для всех событий."""

    _global_subscribers.append(callback)
    log.debug("Subscribed %s to all events", getattr(callback, "__name__", repr(callback)))


def publish(event: Event) -> None:
    """Публикует *event* для всех подписчиков."""

    log.info("Publish event %s attrs=%s", event.kind, event.attrs)
    # Перебираем копию списка, чтобы подписчики могли отписаться внутри коллбэка
    for callback in list(_subscribers.get(event.kind, [])):
        callback(event)
    for callback in list(_global_subscribers):
        callback(event)
