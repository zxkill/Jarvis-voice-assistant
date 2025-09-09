"""Простейший pub/sub‑шлюз для взаимодействия компонентов."""

from collections import defaultdict
from dataclasses import dataclass, field
import logging
import threading
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


# --- Утилиты для проактивных подсказок ------------------------------------
def fire_proactive_trigger(
    kind: str, name: str, context: Dict[str, Any] | None = None
) -> threading.Thread:
    """Асинхронно опубликовать событие проактивного триггера.

    Длительная генерация подсказок через LLM не должна блокировать поток,
    из которого был инициирован триггер (например, цикл чтения камеры).
    Поэтому публикация события выполняется в отдельном **daemon**‑потоке.

    Parameters
    ----------
    kind:
        Тип триггера: ``time``, ``context`` или ``event``.
    name:
        Имя сценария из плейбука.
    context:
        Дополнительные атрибуты, передаваемые обработчику.

    Returns
    -------
    threading.Thread
        Объект запущенного потока, что упрощает тестирование и, при
        необходимости, позволяет дождаться завершения обработки.
    """

    attrs = {"trigger": kind, "name": name}
    if context:
        attrs["context"] = context

    def _worker() -> None:
        """Фактическая публикация события в отдельном потоке."""

        try:
            log.debug(
                "proactivity trigger async publish", extra={"ctx": attrs}
            )
            publish(Event("proactivity.trigger", attrs))
        except Exception:
            # Любая ошибка не должна убивать поток вызывающего кода
            log.exception(
                "proactivity trigger failed", extra={"ctx": attrs}
            )

    thread = threading.Thread(target=_worker, name="proactivity-trigger", daemon=True)
    thread.start()
    return thread
