"""Генерация проактивных подсказок на основе времени."""

from __future__ import annotations

import datetime as dt
from typing import List

from core.events import Event, publish, subscribe
from core.logging_json import configure_logging
from core.metrics import inc_metric, set_metric
from memory.writer import add_suggestion

log = configure_logging("analysis.suggestions")
set_metric("suggestions.queued", 0)

# --- Отслеживание присутствия ---------------------------------------------
# Флаг текущего присутствия пользователя.
_present = False
# Момент последнего ухода пользователя из кадра.
_last_absent = dt.datetime.now()
# Момент последнего напоминания о разминке.
_last_stretch: dt.datetime | None = None


def _on_presence(event: Event) -> None:
    """Обновить внутреннее состояние по событию ``presence.update``."""
    global _present, _last_absent, _last_stretch
    _present = bool(event.attrs.get("present"))
    if not _present:
        # Пользователь ушёл — запоминаем время и сбрасываем таймер растяжки.
        _last_absent = dt.datetime.now()
        _last_stretch = None


# Подписываемся на события присутствия при загрузке модуля.
subscribe("presence.update", _on_presence)


def _emit(text: str, reason_code: str) -> int:
    """Сохранить подсказку и опубликовать событие."""
    suggestion_id = add_suggestion(text)
    log.info(
        "suggestion queued",
        extra={"ctx": {"reason_code": reason_code, "text": text}},
    )
    inc_metric("suggestions.queued")
    publish(
        Event(
            kind="suggestion.created",
            attrs={
                "text": text,
                "reason_code": reason_code,
                "suggestion_id": suggestion_id,
            },
        )
    )
    return suggestion_id


def generate(now: dt.datetime | None = None) -> List[int]:
    """Проверить временные правила и создать подсказки.

    :param now: текущее время, по умолчанию ``datetime.now()``.
    :return: список идентификаторов созданных подсказок.
    """
    global _last_stretch
    now = now or dt.datetime.now()
    created: list[int] = []

    # Около 23:00 напоминаем о будильнике только при присутствии пользователя.
    if now.hour == 23 and now.minute < 5 and _present:
        created.append(_emit("поставить будильник?", "bedtime_alarm"))

    # Напоминание о разминке: пользователь присутствует, активные часы и
    # не отходил от места более часа. Повторяем не чаще раза в час.
    ACTIVE_START_HOUR = 9
    ACTIVE_END_HOUR = 23  # исключая 23:00 и далее
    if (
        _present
        and ACTIVE_START_HOUR <= now.hour < ACTIVE_END_HOUR
        and now - _last_absent >= dt.timedelta(hours=1)
        and (_last_stretch is None or now - _last_stretch >= dt.timedelta(hours=1))
    ):
        created.append(_emit("разминка?", "hourly_stretch"))
        _last_stretch = now

    return created
