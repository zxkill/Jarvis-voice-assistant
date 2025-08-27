from __future__ import annotations

"""Политика выбора эмоциональных атрибутов по координатам настроения.

Функция :func:`select` получает значения ``valence`` и ``arousal`` в диапазоне
``[-1.0; 1.0]`` и возвращает подходящий набор из иконки, TTS‑пресета и
палитры звуковых эффектов.  Для предотвращения «дёрганья» интерфейса
введён минимальный интервал между сменой иконок.
"""

# Стандартные библиотеки
import time
from dataclasses import dataclass
from typing import Dict, Tuple

# Локальные модули
from core.logging_json import configure_logging

# Настраиваем структурированное логирование
_log = configure_logging("emotion.policy")


@dataclass(frozen=True)
class PolicyResult:
    """Результат работы политики выбора эмоций."""

    icon: str
    tts_preset: str
    sfx_palette: str


# Минимальный интервал между сменой иконок в секундах
_MIN_ICON_INTERVAL_SEC = 5.0

# Глобальные переменные для отслеживания последнего переключения
_last_icon: str | None = None
_last_switch_ts: float = 0.0

# Маппинг зон настроения → набор атрибутов.
# Ключ — кортеж (valence_positive, arousal_positive)
#   valence_positive: bool — валентность >= 0
#   arousal_positive: bool — возбуждение >= 0
_ZONE_MAP: Dict[Tuple[bool, bool], PolicyResult] = {
    (True, True): PolicyResult("HAPPY", "cheerful", "bright"),
    (False, True): PolicyResult("ANGRY", "tense", "dark"),
    (False, False): PolicyResult("SAD", "melancholy", "blue"),
    (True, False): PolicyResult("CALM", "soft", "calm"),
}


def select(valence: float, arousal: float) -> PolicyResult:
    """Выбрать атрибуты эмоции на основе ``valence`` и ``arousal``.

    Параметры
    ---------
    valence: float
        Горизонтальная координата настроения (удовольствие ↔ неудовольствие).
    arousal: float
        Вертикальная координата настроения (возбуждение ↔ подавленность).

    Возвращает
    ----------
    PolicyResult
        Структура с иконкой, TTS‑пресетом и SFX‑палитрой.
    """

    global _last_icon, _last_switch_ts

    # Определяем зону по знакам valence и arousal
    key = (valence >= 0.0, arousal >= 0.0)
    result = _ZONE_MAP[key]

    now = time.monotonic()
    switched = False

    if _last_icon != result.icon:
        # Проверяем интервал с предыдущим переключением
        if now - _last_switch_ts < _MIN_ICON_INTERVAL_SEC:
            # Применяем старую иконку, но оставляем новые пресеты и палитру
            _log.debug(
                "icon change throttled", extra={"event": "icon.throttle", "attrs": {"prev": _last_icon, "next": result.icon}}
            )
            result = PolicyResult(_last_icon or result.icon, result.tts_preset, result.sfx_palette)
        else:
            _last_icon = result.icon
            _last_switch_ts = now
            switched = True

    _log.info(
        "selection", extra={"event": "emotion.select", "attrs": {
            "valence": valence,
            "arousal": arousal,
            "icon": result.icon,
            "tts_preset": result.tts_preset,
            "sfx_palette": result.sfx_palette,
            "switched": switched,
        }}
    )

    return result


__all__ = ["select", "PolicyResult"]
