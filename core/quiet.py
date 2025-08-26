from __future__ import annotations
"""Определение и проверка «тихих часов» ассистента.

Модуль вычисляет интервал ночного покоя на основании статистики
присутствия пользователя.  Если агрегатов нет, используются значения из
``config.ini`` или стандартные 23:00–08:00.
"""

import configparser
from dataclasses import dataclass
from datetime import datetime, time
from pathlib import Path
from typing import Iterable

from analysis import habits
from core.logging_json import configure_logging

log = configure_logging("core.quiet")


@dataclass
class QuietHours:
    """Интервал времени, когда ассистент должен вести себя тихо."""

    start: time
    end: time

    def contains(self, moment: datetime | None = None) -> bool:
        """Возвращает ``True``, если *moment* попадает в интервал.

        В интервалах, пересекающих полночь, условие включает время,
        которое больше ``start`` *или* меньше ``end``.
        """

        moment = moment or datetime.now()
        now = moment.time()
        if self.start <= self.end:
            return self.start <= now < self.end
        return now >= self.start or now < self.end


# ---- Параметры алгоритма -------------------------------------------------

# Стандартный ночной период, если статистики нет
DEFAULT_START = time(23, 0)
DEFAULT_END = time(8, 0)

# Порог активности: если за час было менее 15 минут присутствия — считаем его тихим
QUIET_THRESHOLD_SEC = 15 * 60

# Минимальная продолжительность ночи в часах; меньшие периоды считаем шумными
MIN_QUIET_HOURS = 6


def _parse_time(value: str, fallback: time) -> time:
    """Разбирает строку ``HH:MM`` в объект :class:`time`.

    При ошибке возвращает *fallback* и пишет предупреждение в лог.
    """

    try:
        h, m = (int(p) for p in value.split(":", 1))
        return time(h % 24, m % 60)
    except Exception:  # pragma: no cover - защитный механизм
        log.warning("bad time format '%s', using fallback %s", value, fallback)
        return fallback


def _load_config(path: str | Path) -> QuietHours:
    """Прочитать секцию ``[QUIET]`` из конфигурации."""

    cfg = configparser.ConfigParser()
    cfg.read(path, encoding="utf-8")
    start = _parse_time(cfg.get("QUIET", "start", fallback="23:00"), DEFAULT_START)
    end = _parse_time(cfg.get("QUIET", "end", fallback="08:00"), DEFAULT_END)
    log.info(
        "quiet hours from config %s–%s",
        start.strftime("%H:%M"),
        end.strftime("%H:%M"),
    )
    return QuietHours(start=start, end=end)


def derive_quiet_hours(counts: Iterable[int]) -> QuietHours:
    """Рассчитать «тихие часы» по агрегированной активности.

    *counts* — последовательность из 24 чисел, каждое отражает суммарную
    активность (в секундах) за соответствующий час суток.  Алгоритм ищет
    самый длинный непрерывный участок с активностью ниже
    :data:`QUIET_THRESHOLD_SEC`.  Если подходящего участка нет, возвращается
    период по умолчанию.
    """

    data = list(counts)
    if len(data) != 24:  # pragma: no cover - защита от некорректных данных
        log.warning("aggregate length %d != 24, using defaults", len(data))
        return QuietHours(start=DEFAULT_START, end=DEFAULT_END)

    # Дублируем список, чтобы корректно учесть переход через полночь
    extended = data + data
    best_start = 0
    best_len = -1
    current_start = None

    for idx, sec in enumerate(extended):
        if sec < QUIET_THRESHOLD_SEC:
            if current_start is None:
                current_start = idx
        else:
            if current_start is not None:
                length = idx - current_start
                if length > best_len:
                    best_start, best_len = current_start, length
                current_start = None

    # Обработка ситуации, когда тихий период тянется до конца списка
    if current_start is not None:
        length = len(extended) - current_start
        if length > best_len:
            best_start, best_len = current_start, length

    if best_len < MIN_QUIET_HOURS:
        log.info(
            "no quiet segment >= %d h found, using defaults", MIN_QUIET_HOURS
        )
        return QuietHours(start=DEFAULT_START, end=DEFAULT_END)

    start_hour = best_start % 24
    end_hour = (best_start + best_len) % 24
    log.info(
        "derived quiet hours %02d:00–%02d:00 from aggregates", start_hour, end_hour
    )
    return QuietHours(start=time(start_hour, 0), end=time(end_hour, 0))


def update_quiet_hours_from_counts(counts: Iterable[int]) -> QuietHours:
    """Обновить глобальные ``QUIET_HOURS`` на основе агрегатов."""

    global QUIET_HOURS
    QUIET_HOURS = derive_quiet_hours(list(counts))
    return QUIET_HOURS


def refresh_quiet_hours(path: str | Path = "config.ini") -> QuietHours:
    """Перечитать агрегаты и установить новый интервал ``QUIET_HOURS``."""

    counts = habits.load_last_aggregate()
    if counts:
        log.info("using latest aggregates for quiet hours")
        return update_quiet_hours_from_counts(counts)

    log.info("no aggregates found, falling back to config/defaults")
    global QUIET_HOURS
    QUIET_HOURS = _load_config(path)
    return QUIET_HOURS


# При импортировании модуля сразу подгружаем интервал,
# чтобы остальные компоненты могли вызвать ``is_quiet_now`` без инициализации.
QUIET_HOURS = refresh_quiet_hours()


def is_quiet_now() -> bool:
    """Удобная оболочка вокруг ``QUIET_HOURS.contains``."""

    return QUIET_HOURS.contains()

