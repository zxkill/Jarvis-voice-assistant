# -*- coding: utf-8 -*-
"""skills/weather_ru.py — погода с расширенным логированием
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
• Переведён на централизованный логгер (`core.logging_json.configure_logging`).
• Детальные логи: старт/финиш HTTP‑запросов, время отклика, коды ошибок,
  выбранный источник, результаты парсинга, исключения.
• Все `print()` заменены на `log.debug/info/warning/error`.
"""
from __future__ import annotations

import datetime as _dt
import os as _os
import re as _re
import time as _time
from typing import Dict, Tuple

import threading as _th
import requests as _rq

from display import DisplayItem, get_driver
from core.logging_json import configure_logging
from core import events as core_events
from core.events import Event
from context.short_term import add as ctx_add

# ────────────────────────── LOGGING ──────────────────────────────
log = configure_logging(__name__)

# ────────────────────────── CONFIG ───────────────────────────────
LAT: str = _os.getenv("WTTR_LAT", "53.37977235908946")
LON: str = _os.getenv("WTTR_LON", "58.990413992217746")
CITY: str = _os.getenv("JARVIS_CITY", "Магнитогорске")
WTTR_TIMEOUT = 1           # сек
OPENMETEO_TIMEOUT = 4      # сек
FORECAST_DAYS = 3
AUTO_UPDATE_INTERVAL = 1800  # сек для дисплея

_cache_lock = _th.Lock()
_cache_data = {}
_cache_source = ""

_ICON = {
    0: "☀️", 1: "⛅", 2: "☁️",
    62: "🌧️", 73: "🌨️", 45: "🌫️",
}

# WMO weather codes → русские описания (для Open‑Meteо)
WEATHER_CODES_RU: Dict[int, str] = {
    0: "ясно",
    1: "малооблачно",
    2: "переменная облачность",
    3: "пасмурно",
    45: "туман", 48: "сильный туман",
    51: "легкая морось", 53: "морось", 55: "сильная морось",
    56: "ледяная морось", 57: "сильная ледяная морось",
    61: "небольшой дождь", 63: "умеренный дождь", 65: "сильный дождь",
    66: "ледяной дождь", 67: "сильный ледяной дождь",
    71: "небольшой снег", 73: "снег", 75: "сильный снег",
    77: "снежная крупа",
    80: "ливень", 81: "сильный ливень", 82: "очень сильный ливень",
    85: "снежный заряд", 86: "сильный снежный заряд",
    95: "гроза", 96: "гроза с градом", 99: "сильная гроза с градом",
}

WEEKDAYS_RU = ["понедельник","вторник","среда","четверг","пятница","суббота","воскресенье"]

# ─── PATTERNS (оставляем те же) ───────────────────────────────────────────
BASE_PATTERNS = [
    "какая погода", "погода", "погода на улице",
    "что там с погодой", "какая температура", "прогноз погоды",
]
EXTRA = ["завтра", "послезавтра"] + WEEKDAYS_RU
PATTERNS = BASE_PATTERNS + \
    [f"погода {w}" for w in EXTRA] + \
    [f"какая погода {w}" for w in EXTRA] + \
    [f"погода в {d}" for d in WEEKDAYS_RU] + \
    [f"какая погода в {d}" for d in WEEKDAYS_RU]

# ────────────────────────── UTILS ────────────────────────────────

def _plural(n: int, forms: Tuple[str, str, str]) -> str:
    n = abs(n) % 100
    if 11 <= n <= 19:
        return forms[2]
    i = n % 10
    if i == 1:
        return forms[0]
    if 2 <= i <= 4:
        return forms[1]
    return forms[2]


def _detect_offset(text: str) -> int:
    """Определяем, о каком дне спрашивает пользователь."""
    t = text.lower()
    today = _dt.date.today()
    if "послезавтра" in t:
        return 2
    if "завтра" in t:
        return 1
    for delta in range(1, FORECAST_DAYS):
        if WEEKDAYS_RU[(today + _dt.timedelta(days=delta)).weekday()] in t:
            return delta
    m = _re.search(r"(\d{1,2})[.\s](\d{1,2})", t)
    if m:
        day, month = map(int, m.groups())
        try:
            target = _dt.date(today.year, month, day)
            delta = (target - today).days
            if 0 <= delta < FORECAST_DAYS:
                return delta
        except ValueError:
            pass
    return 0

# ────────────────────────── HTTP WRAPPERS ────────────────────────

def _timed_get(url: str, timeout: int) -> _rq.Response:
    t0 = _time.perf_counter()
    resp = _rq.get(url, timeout=timeout)
    dt = (_time.perf_counter() - t0) * 1000
    log.debug("GET %s → %s in %.0f ms", url, resp.status_code, dt)
    resp.raise_for_status()
    return resp

# wttr.in --------------------------------------------------------

def _fetch_wttr(lat: str, lon: str):
    url = f"https://wttr.in/{lat},{lon}?format=j1"
    return _timed_get(url, WTTR_TIMEOUT).json()


def _wttr_desc(obj) -> str:
    return (obj.get("lang_ru", [{}])[0].get("value") or obj["weatherDesc"][0]["value"]).lower()


def _build_answer_wttr(city: str, data, offset: int) -> str:
    if offset == 0:
        cur = data["current_condition"][0]
        temp = round(float(cur["temp_C"]))
        cond_ru = _wttr_desc(cur)
        prefix = "Сейчас"
    else:
        daily = data["weather"][offset]
        tmax = round(float(daily["maxtempC"]))
        tmin = round(float(daily["mintempC"]))
        temp = round((tmax + tmin) / 2)
        cond_ru = _wttr_desc(daily["hourly"][4 if len(daily["hourly"]) > 4 else 0])
        prefix = ["Сегодня", "Завтра", "Послезавтра"][offset] if offset < 3 else (
            f"В {WEEKDAYS_RU[(_dt.date.today() + _dt.timedelta(days=offset)).weekday()]}"
        )
    degree = _plural(temp, ("градус", "градуса", "градусов"))
    sign = "тепла" if temp > 0 else ("мороза" if temp < 0 else "")
    return f"{prefix} в {city} {abs(temp)} {degree} {sign}, {cond_ru}.".strip()

# Open‑Meteo ------------------------------------------------------

def _fetch_openmeteo(lat: str, lon: str):
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        "&current=temperature_2m,weather_code"
        "&daily=weather_code,temperature_2m_max,temperature_2m_min"
        f"&forecast_days={FORECAST_DAYS}&timezone=auto"
    )
    return _timed_get(url, OPENMETEO_TIMEOUT).json()


def _build_answer_openmeteo(city: str, data, offset: int) -> str:
    if offset == 0:
        temp = round(data["current"]["temperature_2m"])
        code = data["current"]["weather_code"]
        prefix = "Сейчас"
    else:
        idx = offset
        tmax = data["daily"]["temperature_2m_max"][idx]
        tmin = data["daily"]["temperature_2m_min"][idx]
        temp = round((tmax + tmin) / 2)
        code = data["daily"]["weather_code"][idx]
        prefix = ["Сегодня", "Завтра", "Послезавтра"][offset] if offset < 3 else (
            f"В {WEEKDAYS_RU[(_dt.date.today() + _dt.timedelta(days=offset)).weekday()]}"
        )
    cond_ru = WEATHER_CODES_RU.get(code, "")
    degree = _plural(temp, ("градус", "градуса", "градусов"))
    sign = "тепла" if temp > 0 else ("мороза" if temp < 0 else "")
    return f"{prefix} в {city} {abs(temp)} {degree} {sign}, {cond_ru}.".strip()


def _update_cache() -> None:
    global _cache_data, _cache_source
    try:
        log.debug("Updating weather cache from wttr.in")
        data = _fetch_wttr(LAT, LON)
        source = "wttr"
    except Exception as exc:
        log.warning("Wttr failed: %s — fallback to OpenMeteo", exc)
        data = _fetch_openmeteo(LAT, LON)
        source = "openmeteo"
    with _cache_lock:
        _cache_data = data
        _cache_source = source


def _calc_mood_coefficients(temp: int, cond: str) -> Tuple[float, float]:
    """Рассчитать коэффициенты влияния погоды на настроение."""
    valence = 0.0
    arousal = 0.0
    cond_l = cond.lower()
    if any(w in cond_l for w in ["дожд", "ливень", "морось", "снег"]):
        valence -= 1.0
        arousal -= 0.5
    elif any(w in cond_l for w in ["ясно", "солнечно"]):
        valence += 0.5
    if temp <= 0:
        valence -= 0.5
        arousal -= 0.5
    elif temp >= 25:
        valence += 0.5
        arousal += 0.5
    log.debug(
        "Mood coefficients: valence=%+.2f arousal=%+.2f",
        valence,
        arousal,
        extra={"ctx": {"temperature": temp, "condition": cond}},
    )
    return valence, arousal


def _update_context(temp: int, cond: str) -> None:
    """Опубликовать погодные данные в событийную шину и контекст."""
    val, ar = _calc_mood_coefficients(temp, cond)
    core_events.publish(Event(kind="weather.update", attrs={"valence": val, "arousal": ar}))
    try:
        ctx_add(
            {
                "source": "weather",
                "temperature": temp,
                "condition": cond,
                "valence": val,
                "arousal": ar,
            }
        )
    except Exception:
        log.exception("Не удалось добавить погодные данные в контекст")
    log.info(
        "Weather context updated",
        extra={
            "ctx": {
                "temperature": temp,
                "condition": cond,
                "valence": val,
                "arousal": ar,
            }
        },
    )

# ────────────────────────── PUBLIC API ───────────────────────────

def handle(text: str) -> str:
    """Основной скилл‑хендлер."""
    offset = _detect_offset(text)
    answer = _build_answer(offset)
    # Дополнительно формируем подсказки и публикуем контекст
    temp, cond = _conditions(offset)
    hint = _clothing_hint(temp, cond)
    if hint:
        answer = f"{answer} {hint}"
    _update_context(temp, cond)
    log.info("Weather answer: %s", answer)
    return answer


def auto_update():
    """Периодический апдейт температуры и обновление кэша."""
    _update_cache()
    temp, icon = _current_for_display()
    log.debug("Display update: %d°C %s", temp, icon)
    drv = get_driver()
    drv.draw(DisplayItem(kind="weather", payload=f"{temp:02d}°C {icon}"))
    # Также сообщаем о погоде в контекст и эмоции
    temp_c, cond = _conditions(0)
    _update_context(temp_c, cond)

# ────────────────────────── INTERNALS ────────────────────────────

def _current_for_display() -> Tuple[int, str]:
    with _cache_lock:
        data = _cache_data
        source = _cache_source
    if not data:
        _update_cache()
        with _cache_lock:
            data = _cache_data
            source = _cache_source
    try:
        if source == "wttr":
            cur = data["current_condition"][0]
            temp = round(float(cur["temp_C"]))
            code = int(cur.get("weatherCode", 0))
        else:
            cw = data.get("current", {})
            temp = round(float(cw.get("temperature_2m", 0)))
            code = int(cw.get("weathercode", cw.get("weather_code", 0)))
        icon = _ICON.get(code, "")
        return temp, icon
    except Exception as exc:
        log.error("Display cache error: %s", exc)
        return 0, ""


def _conditions_wttr(data, offset: int) -> Tuple[int, str]:
    """Извлечь температуру и описание погоды из ответа wttr.in."""
    if offset == 0:
        cur = data["current_condition"][0]
        temp = round(float(cur["temp_C"]))
        cond_ru = _wttr_desc(cur)
    else:
        daily = data["weather"][offset]
        tmax = round(float(daily["maxtempC"]))
        tmin = round(float(daily["mintempC"]))
        temp = round((tmax + tmin) / 2)
        cond_ru = _wttr_desc(daily["hourly"][4 if len(daily["hourly"]) > 4 else 0])
    return temp, cond_ru


def _conditions_openmeteo(data, offset: int) -> Tuple[int, str]:
    """Извлечь температуру и описание погоды из Open‑Meteo."""
    if offset == 0:
        temp = round(data["current"]["temperature_2m"])
        code = data["current"]["weather_code"]
    else:
        idx = offset
        tmax = data["daily"]["temperature_2m_max"][idx]
        tmin = data["daily"]["temperature_2m_min"][idx]
        temp = round((tmax + tmin) / 2)
        code = data["daily"]["weather_code"][idx]
    cond_ru = WEATHER_CODES_RU.get(code, "")
    return temp, cond_ru


def _conditions(offset: int = 0) -> Tuple[int, str]:
    """Получить температуру и словесное описание погоды из кэша."""
    with _cache_lock:
        data = _cache_data
        source = _cache_source
    if not data:
        _update_cache()
        with _cache_lock:
            data = _cache_data
            source = _cache_source
    if source == "wttr":
        return _conditions_wttr(data, offset)
    return _conditions_openmeteo(data, offset)


def _clothing_hint(temp: int, cond: str) -> str:
    """Сформировать подсказку по одежде и необходимости зонта."""
    cond_l = cond.lower()
    hints = []
    if temp <= 0:
        hints.append("оденься теплее")
    elif temp < 15:
        hints.append("возьми лёгкую куртку")
    elif temp >= 25:
        hints.append("надень что-нибудь полегче")
    if any(w in cond_l for w in ["дожд", "ливень", "морось"]):
        hints.append("не забудь зонт")
    elif "снег" in cond_l:
        hints.append("захвати зонт")
    if hints:
        return (" ".join(hints)).capitalize() + "."
    return ""


def _build_answer(offset: int = 0) -> str:
    with _cache_lock:
        data = _cache_data
        source = _cache_source
    if not data:
        log.debug("Weather cache empty, updating synchronously")
        _update_cache()
        with _cache_lock:
            data = _cache_data
            source = _cache_source
    if source == "wttr":
        log.debug("Using wttr.in cached data")
        return _build_answer_wttr(CITY, data, offset)
    log.debug("Using Open‑Meteo cached data")
    return _build_answer_openmeteo(CITY, data, offset)


# Экспортируем функцию для тестов и других модулей
calc_mood_coefficients = _calc_mood_coefficients
