"""Простейший реестр метрик, хранящий данные в памяти процесса."""

from __future__ import annotations

from collections import defaultdict
from typing import Dict

from core.logging_json import configure_logging

# Логгер метрик.
log = configure_logging("metrics")

# Хранилище значений метрик. ``defaultdict`` автоматически
# инициализирует отсутствующие ключи значением ``0.0``.
_metrics: Dict[str, float] = defaultdict(float)


def set_metric(name: str, value: float) -> None:
    """Установить значение *name* для метрики‑«гейджа»."""
    _metrics[name] = float(value)
    log.info(
        "metric set",
        extra={"event": "metric", "attrs": {"name": name, "value": _metrics[name]}},
    )


def inc_metric(name: str, value: float = 1.0) -> None:
    """Увеличить счётчик *name* на указанное *value*."""
    _metrics[name] += float(value)
    log.info(
        "metric inc",
        extra={"event": "metric", "attrs": {"name": name, "value": _metrics[name]}},
    )


def get_metric(name: str) -> float:
    """Получить текущее значение метрики *name* (0.0 при отсутствии)."""
    return _metrics.get(name, 0.0)


def snapshot() -> Dict[str, float]:
    """Вернуть копию всех метрик для экспорта или отладки."""
    return dict(_metrics)
