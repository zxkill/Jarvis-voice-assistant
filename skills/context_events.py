"""Скилл для добавления и получения событий дня из долгосрочного контекста."""

from __future__ import annotations

from context.long_term import add_daily_event, get_events_by_label

PATTERNS = [
    "запомни",  # добавление события
    "события дня",  # запрос сохранённых событий
    "что запомнил",
]

LABEL = "note"


def handle(text: str) -> str:
    low = text.lower().strip()
    if low.startswith("запомни"):
        event = text[len("запомни") :].strip()
        if not event:
            return "Что запомнить?"
        add_daily_event(event, [LABEL])
        return "Запомнил"

    events = get_events_by_label(LABEL)
    if not events:
        return "Пока ничего не запомнил"
    return "; ".join(events)
