"""Хранение актуальной даты в контексте.

Модуль обеспечивает централизованное хранение текущей даты, чтобы
различные подсистемы ассистента могли обращаться к ней без повторных
вызовов ``datetime.date.today``.  Это позволяет избегать расхождений в
контексте и упрощает тестирование, так как дату можно подменить.
"""

from __future__ import annotations

import datetime as dt
import logging

# Логгер модуля для детальной отладки
log = logging.getLogger(__name__)

# Внутреннее состояние: последняя известная дата
_current: dt.date | None = None


def refresh() -> dt.date:
    """Обновить и вернуть текущую дату.

    Если сохранённая дата отличается от системной, она обновляется.  Это
    особенно важно после наступления полуночи, когда нужно сбросить
    контекст на новый день.
    """

    global _current
    today = dt.date.today()
    if _current != today:
        _current = today
        log.debug(
            "context.current_date: дата обновлена",
            extra={"ctx": {"date": _current.isoformat()}},
        )
    return _current


def get() -> dt.date:
    """Получить дату из контекста, не обращаясь к системному времени."""
    if _current is None:
        return refresh()
    return _current
