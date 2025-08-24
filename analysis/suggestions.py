"""Генерация проактивных подсказок на основе времени."""

from __future__ import annotations

import datetime as dt
from typing import List

from core.events import Event, publish
from core.logging_json import configure_logging
from core.metrics import inc_metric, set_metric
from memory.writer import add_suggestion

log = configure_logging("analysis.suggestions")
set_metric("suggestions.queued", 0)


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
    now = now or dt.datetime.now()
    created: list[int] = []

    # Около 23:00 — предложение поставить будильник
    if now.hour == 23 and now.minute < 5:
        created.append(_emit("поставить будильник?", "bedtime_alarm"))

    # В начале каждого часа — предложение разминки
    if now.minute == 0:
        created.append(_emit("разминка?", "hourly_stretch"))

    return created
