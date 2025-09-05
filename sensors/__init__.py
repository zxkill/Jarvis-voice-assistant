"""Модули, отвечающие за работу датчиков ассистента.

Здесь реализованы механизмы явного согласия пользователя на использование
камеры и микрофона, а также отображение индикаторов активности через модуль
``display``. Все функции снабжены подробным логированием для упрощения
отладки и мониторинга работы системы.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict

from display import DisplayItem, get_driver

# Логгер модуля
log = logging.getLogger(__name__)


@dataclass
class _SensorState:
    """Хранит состояние согласия и активности сенсора."""

    consent: bool = False
    active: bool = False


# Глобальный реестр сенсоров
_SENSORS: Dict[str, _SensorState] = {
    "camera": _SensorState(),
    "microphone": _SensorState(),
}


def grant_consent(sensor: str) -> None:
    """Зафиксировать согласие пользователя на использование сенсора."""

    state = _SENSORS.setdefault(sensor, _SensorState())
    state.consent = True
    log.info("Получено согласие на использование %s", sensor)


def revoke_consent(sensor: str) -> None:
    """Отозвать согласие пользователя."""

    state = _SENSORS.setdefault(sensor, _SensorState())
    state.consent = False
    state.active = False
    # При отзыве согласия выключаем индикатор
    get_driver().draw(DisplayItem(kind=sensor, payload=None))
    log.warning("Согласие на %s отозвано", sensor)


def _ensure_consent(sensor: str) -> None:
    """Проверить наличие согласия, иначе выбросить ``PermissionError``."""

    state = _SENSORS.setdefault(sensor, _SensorState())
    if not state.consent:
        log.error("Попытка доступа к %s без явного согласия", sensor)
        raise PermissionError(f"Нет согласия на использование {sensor}")


def set_active(sensor: str, active: bool) -> None:
    """Включить или выключить сенсор с обновлением индикатора."""

    _ensure_consent(sensor)
    state = _SENSORS.setdefault(sensor, _SensorState())
    state.active = active
    icon = "📷" if sensor == "camera" else "🎤"
    payload = icon if active else None
    get_driver().draw(DisplayItem(kind=sensor, payload=payload))
    log.info("%s %s", sensor, "активен" if active else "неактивен")


def is_active(sensor: str) -> bool:
    """Проверить, активен ли сенсор."""

    return _SENSORS.get(sensor, _SensorState()).active


__all__ = [
    "grant_consent",
    "revoke_consent",
    "set_active",
    "is_active",
]

