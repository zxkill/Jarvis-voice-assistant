# -*- coding: utf-8 -*-
"""
Скилл «Таймер, будильник и напоминание» (RU)
===========================================

Понимает фразы вида
-------------------
• «поставь таймер на 5 минут [пицца]»
• «разбуди в 7:30 [подъём]»
• «напомни мне через 10 минут позвонить маме»
• «останови таймер [пицца]» или «отмени таймер [пицца]»
• «какие таймеры/будильники/напоминания установлены»

Поддерживает несколько таймеров, будильников и напоминаний с произвольными метками.
При срабатывании проигрывает короткий сигнал, выводит эмоцию SURPRISED
и озвучивает сообщение.
"""

from __future__ import annotations
import datetime as _dt
import re
import threading
import time
from pathlib import Path
from typing import Dict, Tuple

from display import get_driver, DisplayItem
from emotion.state import Emotion
from memory.db import get_connection
from notifiers.telegram import send as _tg_send
from core import stop as _stop_mgr
from core.logging_json import configure_logging

log = configure_logging("skills.timer_alarm")

# ─────────────────────────────────────────────────────────────────────────────
PATTERNS = [
    "поставь таймер",
    "разбуди",
    "напомни",
    "останови таймер",
    "останови будильник",
    "останови напоминание",
    "отмени таймер",
    "отмени будильник",
    "отмени напоминание",
    "какие таймеры",
    "какие будильники",
    "какие напоминания",
    "покажи таймеры",
    "покажи будильники",
    "покажи напоминания",
]

# Активные задачи: метка -> (Timer, тип, момент окончания)
# тип: timer | alarm | reminder_timer | reminder_alarm
_TIMERS: Dict[str, Tuple[threading.Timer, str, _dt.datetime]] = {}

# Сработавшие, но ещё не подтверждённые таймеры: метка -> (тип, событие остановки)
_ALERTS: Dict[str, Tuple[str, threading.Event]] = {}

# Путь к пользовательскому звуку будильника/напоминания
_ALARM_WAV = Path(__file__).resolve().parent.parent / "audio" / "sfx" / "alarm.wav"


def _save_timer(label: str, typ: str, end: _dt.datetime) -> None:
    """Сохраняет информацию о таймере в SQLite."""
    with get_connection() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO timers(label, typ, end_ts) VALUES (?, ?, ?)",
            (label, typ, int(end.timestamp())),
        )


def _remove_timer(label: str | None) -> None:
    """Удаляет таймер(ы) из базы."""
    with get_connection() as conn:
        if label is None:
            conn.execute("DELETE FROM timers")
        else:
            conn.execute("DELETE FROM timers WHERE label=?", (label,))

# Словарь единиц времени для перевода в секунды
_UNITS = {
    "сек": 1,
    "секунд": 1,
    "секунду": 1,
    "секунды": 1,
    "мин": 60,
    "минут": 60,
    "минуту": 60,
    "минуты": 60,
    "час": 3600,
    "часа": 3600,
    "часов": 3600,
}

_NUM_WORDS = {
    "ноль": 0, "ноля": 0,
    "один": 1, "одна": 1, "одну": 1, "одной": 1,
    "два": 2, "две": 2,
    "три": 3,
    "четыре": 4,
    "пять": 5,
    "шесть": 6,
    "семь": 7,
    "восемь": 8,
    "девять": 9,
    "десять": 10,
    "одиннадцать": 11,
    "двенадцать": 12,
    "тринадцать": 13,
    "четырнадцать": 14,
    "пятнадцать": 15,
    "шестнадцать": 16,
    "семнадцать": 17,
    "восемнадцать": 18,
    "девятнадцать": 19,
    "двадцать": 20,
    "тридцать": 30,
    "сорок": 40,
    "пятьдесят": 50,
    "шестьдесят": 60,
}


def _words_to_number(chunk: str) -> int | None:
    """Конвертирует "двадцать пять" → 25."""
    numbers: list[int] = []
    acc: int | None = None
    for w in chunk.lower().split():
        if w.isdigit():
            numbers.append(int(w))
            acc = None
            continue
        val = _NUM_WORDS.get(w)
        if val is None:
            if acc is not None:
                numbers.append(acc)
                acc = None
            continue
        if val < 10 and acc is not None and acc >= 20:
            numbers.append(acc + val)
            acc = None
        elif val % 10 == 0 and val >= 20:
            acc = val
        else:
            numbers.append(val)
            acc = None
    if acc is not None:
        numbers.append(acc)
    return numbers[0] if numbers else None


_DUR_RE = re.compile(r"(?:на|через)\s+(?P<num>[\dа-яё\s]+?)\s+(?P<unit>[а-я]+)")
_TIME_RE = re.compile(r"(?:в|на)\s*(?P<h>\d{1,2})(?:[:.\s](?P<m>\d{1,2}))?")


def _to_int(tok: str) -> int | None:
    """Преобразует отдельное слово или число в int."""
    tok = tok.strip()
    return int(tok) if tok.isdigit() else _words_to_number(tok)


def _beep(freq: int = 880, duration: float = 0.2) -> None:
    """Проигрывает звуковой сигнал.

    Если в ``audio/sfx/alarm.wav`` присутствует пользовательский WAV‑файл,
    воспроизводим его. В противном случае генерируем короткий синусоидальный
    сигнал для обратной совместимости.

    ``numpy`` и ``sounddevice`` доступны не во всех средах (например, в тестах
    на CI). Чтобы модуль можно было импортировать без этих зависимостей,
    выполняем импорты лениво и при ошибке просто выходим.
    """
    try:  # зависимости могут отсутствовать
        import numpy as _np  # type: ignore
        import sounddevice as _sd  # type: ignore
    except Exception:
        return

    if _ALARM_WAV.exists():
        try:
            import wave

            with wave.open(str(_ALARM_WAV), "rb") as wf:
                sample_rate = wf.getframerate()
                frames = wf.readframes(wf.getnframes())
                data = _np.frombuffer(frames, dtype=_np.int16)
                if wf.getnchannels() > 1:
                    data = data.reshape(-1, wf.getnchannels())
                _sd.play(data, sample_rate)
                _sd.wait()
                return
        except Exception:
            pass  # при ошибке откатываемся к генерации синусоиды

    sample_rate = 44100  # частота дискретизации
    t = _np.linspace(0, duration, int(sample_rate * duration), False)
    tone = _np.sin(freq * 2 * _np.pi * t) * 0.2
    _sd.play(tone, sample_rate)
    _sd.wait()


def _speak(msg: str) -> None:
    """Озвучивает текст с помощью встроенного TTS."""
    from working_tts import working_tts
    working_tts(msg)


def _fire(label: str, typ: str) -> None:
    """Логика срабатывания таймера/будильника/напоминания."""

    log.info("timer fired: label=%s typ=%s", label, typ)
    _TIMERS.pop(label, None)  # объект ``Timer`` больше не нужен
    present = _user_present()
    log.info("user_present=%s", present)

    if present:
        # Пользователь рядом — озвучиваем и повторяем сигнал до остановки
        _beep()
        driver = get_driver()
        driver.draw(DisplayItem(kind="emotion", payload=Emotion.SURPRISED.value))
        if typ.startswith("reminder"):
            _speak(f"Напоминание {label}")
        else:
            kind = "Будильник" if typ == "alarm" else "Таймер"
            _speak(f"{kind} {label} сработал")
        stop_event = threading.Event()
        _ALERTS[label] = (typ, stop_event)
        threading.Thread(target=_alert_loop, args=(stop_event,), daemon=True).start()
    else:
        # Пользователя нет — отправляем сообщение в Telegram и очищаем запись
        if typ.startswith("reminder"):
            msg = f"Напоминание {label}"
        else:
            kind = "Будильник" if typ == "alarm" else "Таймер"
            msg = f"{kind} {label} сработал"
        log.info("sending telegram: %s", msg)
        _tg_send(msg)
        _remove_timer(label)


def _user_present() -> bool:
    """Проверяем по БД, находится ли пользователь рядом с ассистентом."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT end_ts FROM presence_sessions ORDER BY id DESC LIMIT 1"
        ).fetchone()
    present = row is not None and row["end_ts"] is None
    log.info("presence check: row=%s present=%s", row, present)
    return present


def _alert_loop(stop_event: threading.Event) -> None:
    """Подает звуковые сигналы, пока таймер не будет остановлен."""
    while not stop_event.is_set():
        _beep()
        time.sleep(1.5)


def _schedule(seconds: int, label: str, typ: str, save: bool = True) -> None:
    """Создаёт и запускает таймер/будильник."""
    t = threading.Timer(seconds, _fire, args=(label, typ))
    t.daemon = True  # позволяет завершить программу с активными таймерами
    end = _dt.datetime.now() + _dt.timedelta(seconds=seconds)
    _TIMERS[label] = (t, typ, end)  # сохраняем для последующего управления
    t.start()
    if save:
        _save_timer(label, typ, end)


def _restore_from_db() -> None:
    """При старте восстанавливает активные и просроченные таймеры."""
    now = int(_dt.datetime.now().timestamp())
    with get_connection() as conn:
        for row in conn.execute("SELECT label, typ, end_ts FROM timers"):
            if row["end_ts"] > now:
                sec = row["end_ts"] - now
                _schedule(sec, row["label"], row["typ"], save=False)
            else:
                # Таймер уже истёк во время простоя программы
                _fire(row["label"], row["typ"])


# При импорте навыка восстанавливаем незавершённые таймеры
_restore_from_db()


def _parse_duration(text: str, default: str) -> Tuple[int, str] | None:
    """Парсит относительную длительность таймера/напоминания."""
    m = _DUR_RE.search(text)
    if not m:
        return None  # не нашли количество
    num_token = m.group("num").strip()
    unit = m.group("unit")
    sec_per = _UNITS.get(unit)  # переводим единицу в секунды
    if not sec_per:
        return None
    num = _to_int(num_token)
    if num is None:
        return None
    sec = num * sec_per
    # остаток строки после совпадения считаем меткой
    label = text[m.end():].strip() or default
    return sec, label


def _parse_time(text: str, default: str) -> Tuple[int, str] | None:
    """Парсит абсолютное время для будильника/напоминания."""
    m = _TIME_RE.search(text)
    h: int | None = None
    mnt: int = 0
    end_idx: int | None = None
    if m:
        after = text[m.end():].lstrip()
        if after.startswith("час"):
            m = None
        else:
            h = int(m.group("h"))
            mnt = int(m.group("m") or 0)
            end_idx = m.end()
    if not m:
        m2 = re.search(r"(?:в|на)\s+(.+)", text)
        if not m2:
            return None
        tokens = m2.group(1).split()
        if not tokens:
            return None
        h = _to_int(tokens[0])
        if h is None:
            return None
        idx = 1
        if idx < len(tokens):
            t = tokens[idx]
            if t.startswith("час"):
                idx += 1
                if idx < len(tokens):
                    cand = _to_int(tokens[idx])
                    if cand is not None:
                        mnt = cand
                        idx += 1
                        if idx < len(tokens) and tokens[idx].startswith("мин"):
                            idx += 1
            else:
                cand = _to_int(t)
                if cand is not None:
                    mnt = cand
                    idx += 1
                    if idx < len(tokens) and tokens[idx].startswith("мин"):
                        idx += 1
        label = " ".join(tokens[idx:]).strip() or default
        now = _dt.datetime.now()
        alarm = now.replace(hour=h, minute=mnt, second=0, microsecond=0)
        if alarm <= now:
            alarm += _dt.timedelta(days=1)
        sec = int((alarm - now).total_seconds())
        return sec, label
    now = _dt.datetime.now()
    alarm = now.replace(hour=h, minute=mnt, second=0, microsecond=0)
    if alarm <= now:
        alarm += _dt.timedelta(days=1)
    sec = int((alarm - now).total_seconds())
    label = text[end_idx:].strip() or default
    return sec, label


def _list_timers() -> str:
    """Возвращает список активных и сработавших таймеров."""
    if not _TIMERS and not _ALERTS:
        return "Активных таймеров и напоминаний нет"
    now = _dt.datetime.now()
    lines: list[str] = []
    for label, (_, typ, end) in _TIMERS.items():
        if typ in ("timer", "reminder_timer"):
            rem = int((end - now).total_seconds())
            m, s = divmod(max(rem, 0), 60)
            kind = "Таймер" if typ == "timer" else "Напоминание"
            if m:
                lines.append(f"{kind} {label}: {m} мин {s} с")
            else:
                lines.append(f"{kind} {label}: {s} с")
        else:
            kind = "Будильник" if typ == "alarm" else "Напоминание"
            lines.append(f"{kind} {label}: {end.strftime('%H:%M')}")
    for label, (typ, _) in _ALERTS.items():
        if typ.startswith("reminder"):
            kind = "Напоминание"
        else:
            kind = "Будильник" if typ == "alarm" else "Таймер"
        lines.append(f"{kind} {label}: истёк, ждёт подтверждения")
    driver = get_driver()
    driver.draw(DisplayItem(kind="text", payload="\n".join(lines)))
    return ". ".join(lines)


def _stop(label: str | None) -> str:
    """Останавливает указанную задачу или все сразу."""
    if not _TIMERS and not _ALERTS:
        return "Активных таймеров и напоминаний нет"
    if label:
        timer = _TIMERS.pop(label, None)
        if timer:
            timer[0].cancel()
            _remove_timer(label)
            typ = timer[1]
            if typ.startswith("reminder"):
                kind = "Напоминание"
            else:
                kind = "Будильник" if typ == "alarm" else "Таймер"
            return f"{kind} {label} остановлен"
        alert = _ALERTS.pop(label, None)
        if alert:
            alert[1].set()
            _remove_timer(label)
            if alert[0].startswith("reminder"):
                kind = "Напоминание"
            else:
                kind = "Будильник" if alert[0] == "alarm" else "Таймер"
            return f"{kind} {label} остановлен"
        return f"Задача {label} не найдена"
    # если метка не указана — останавливаем все задачи
    for t, _, _ in _TIMERS.values():
        t.cancel()
    for _, ev in _ALERTS.values():
        ev.set()
    _TIMERS.clear()
    _ALERTS.clear()
    _remove_timer(None)
    return "Все таймеры и напоминания остановлены"


def _stop_handler() -> bool:
    """Коллбэк для глобальной команды «стоп».

    Останавливает первый сработавший таймер/напоминание, если такой есть.
    Возвращает ``True``, если что-то было остановлено.
    """

    if _ALERTS:
        label = next(iter(_ALERTS))
        _stop(label)
        return True
    return False


_stop_mgr.register(_stop_handler)


def handle(text: str) -> str:
    """Главная точка входа навыка."""
    txt = text.lower()
    # остановка или отмена таймера/будильника/напоминания
    if "останови" in txt or "отмени" in txt:
        m = re.search(r"(?:останови|отмени) (?:таймер|будильник|напоминание)(?: (?P<label>[\wа-я]+))?", txt)
        label = m.group("label") if m else None
        return _stop(label)
    # запрос списка активных задач
    if ("какие" in txt or "покажи" in txt) and ("таймер" in txt or "будильник" in txt or "напоминание" in txt):
        return _list_timers()
    # постановка таймера с относительной длительностью
    if "поставь" in txt and "таймер" in txt:
        parsed = _parse_duration(txt, "таймер")
        if not parsed:
            return "Не понял длительность, повторите"
        sec, label = parsed
        if label in _TIMERS:
            _TIMERS[label][0].cancel()
            _schedule(sec, label, "timer")
            return f"Перезапускаю таймер {label}"
        _schedule(sec, label, "timer")
        return f"Таймер {label} на {sec // 60 if sec >= 60 else sec} {'минут' if sec >= 60 else 'секунд'} запущен"
    # установка будильника на конкретное время
    if "разбуди" in txt:
        parsed = _parse_time(txt, "будильник")
        if not parsed:
            return "Не понял время, повторите"
        sec, label = parsed
        if label in _TIMERS:
            _TIMERS[label][0].cancel()
            _schedule(sec, label, "alarm")
            return f"Перезапускаю будильник {label}"
        _schedule(sec, label, "alarm")
        return f"Будильник {label} установлен"
    # установка напоминания
    if "напомни" in txt:
        parsed = _parse_duration(txt, "напоминание")
        typ = "reminder_timer"
        if not parsed:
            parsed = _parse_time(txt, "напоминание")
            typ = "reminder_alarm"
        if not parsed:
            return "Не понял время, повторите"
        sec, label = parsed
        if label in _TIMERS:
            _TIMERS[label][0].cancel()
            _schedule(sec, label, typ)
            return f"Перезапускаю напоминание {label}"
        _schedule(sec, label, typ)
        if typ == "reminder_timer":
            if sec >= 3600:
                return f"Напоминание {label} через {sec // 3600} часов"
            if sec >= 60:
                return f"Напоминание {label} через {sec // 60} минут"
            return f"Напоминание {label} через {sec} секунд"
        end = (_dt.datetime.now() + _dt.timedelta(seconds=sec)).strftime('%H:%M')
        return f"Напоминание {label} на {end} установлено"
    return ""
