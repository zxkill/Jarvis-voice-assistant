"""Драйвер звуковых эмоций, использующий готовые файлы.

Читает конфигурацию ``audio/sfx_manifest.yaml`` и при получении события
``emotion_changed`` выбирает случайный звуковой файл для соответствующей
эмоции.  Файлы должны быть в формате WAV (mono, 44.1 kHz), однако при
отсутствии необходимых зависимостей или файла звук просто пропускается.
"""

from __future__ import annotations

import random
import time
import wave
import inspect
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Any, Optional
from threading import Lock

try:  # ``numpy`` может быть недоступен в некоторых средах
    import numpy as np  # type: ignore
except Exception:  # pragma: no cover - необязательная зависимость
    np = None  # type: ignore

try:  # ``yaml`` may be missing in minimal environments
    import yaml  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    yaml = None  # type: ignore

from emotion.state import Emotion
from core.logging_json import configure_logging
from core import events as core_events
from core.quiet import is_quiet_now
from utils.rate_limiter import RateLimiter

try:  # ``sounddevice`` может быть недоступен в среде тестирования
    import sounddevice as sd  # type: ignore
except Exception:  # pragma: no cover
    sd = None  # type: ignore


MANIFEST_PATH = Path(__file__).resolve().parent.parent / "audio" / "sfx_manifest.yaml"
log = configure_logging("emotion.sound")


# Глобальный кеш эффектов, позволяющий отслеживать время последнего
# воспроизведения и тем самым исключать многократное повторение звука,
# например, «вздоха».
_EFFECTS: Dict[str, _Effect] | None = None

# Текущая палитра звуковых эффектов, задаётся менеджером настроения.
_CURRENT_PALETTE: str = ""
# Глобальный лимитер частоты воспроизведения любых эффектов.
_GLOBAL_LIMITER: RateLimiter | None = None


# соответствие некоторых эмоций и строковых ключей записи в манифесте
# Используем тип Any, чтобы словарь мог принимать как элементы Enum,
# так и произвольные строковые события.  Это позволяет единообразно
# обращаться к эффектам как из внутренних событий эмоций, так и при
# явном вызове ``play_effect`` с именованным ключом.
_ALIASES: Dict[Any, str] = {
    Emotion.NEUTRAL: "IDLE",          # стандартный фон при простое
    "IDLE_BREATH": "IDLE_BREATH",   # короткое дыхание во время простоя
    "WAKE": "WAKE",                 # приветствие при запуске
    "YAWN": "YAWN",                 # явный вызов зевка
    "SIGH": "SIGH",                 # явный вызов вздоха
}

# Минимальный интервал для повторного воспроизведения "дыхания".
# Значение в секундах, соответствует 15 минутам.  Даже если в YAML-файле
# указан меньший ``cooldown``, мы принудительно увеличим его до этого
# порога, чтобы избежать навязчивых повторов звука.
MIN_IDLE_BREATH_COOLDOWN = 15 * 60

# Временная метка последнего глобального воспроизведения "дыхания".
# Используется, чтобы исключить повтор эффекта при наличии нескольких
# экземпляров драйвера или прямых вызовов `play_effect("IDLE_BREATH")`.
_idle_breath_last: float = 0.0
# Общая блокировка позволяет атомарно проверять и обновлять значение
# `_idle_breath_last` между потоками.
_idle_breath_lock = Lock()

@dataclass
class _Effect:
    files: List[str]
    gain: float
    cooldown: float
    repeat: int = 1  # сколько раз подряд воспроизводить эффект
    last_played: float = 0.0
    lock: Lock = field(default_factory=Lock, repr=False)


def _load_manifest() -> Dict[str, _Effect]:
    if not MANIFEST_PATH.exists() or yaml is None:
        return {}
    try:
        data = yaml.safe_load(MANIFEST_PATH.read_text("utf-8")) or {}
    except Exception:  # pragma: no cover - повреждённый YAML
        return {}

    global _GLOBAL_LIMITER

    # Извлекаем глобальный лимит частоты воспроизведения.
    rate_ms = float(data.pop("global_rate_limit_ms", 0))
    if rate_ms > 0:
        _GLOBAL_LIMITER = RateLimiter(1, rate_ms / 1000.0)

    effects: Dict[str, _Effect] = {}
    for name, cfg in data.items():
        if not isinstance(cfg, dict):
            continue
        files = [str(f) for f in cfg.get("files", [])]
        gain = float(cfg.get("gain_db", 0))
        cooldown = float(cfg.get("cooldown_ms", 0)) / 1000.0
        repeat = int(cfg.get("repeat", 1))
        if isinstance(name, bool):
            key = "YES" if name else "NO"
        else:
            key = str(name).upper()
        effects[key] = _Effect(files=files, gain=gain, cooldown=cooldown, repeat=repeat)
    # Принудительно повышаем ``cooldown`` для дыхания, если он меньше
    # минимального допустимого значения.  Это защищает от случайного
    # указания слишком маленького интервала в конфигурации.
    breath = effects.get("IDLE_BREATH")
    if breath and breath.cooldown < MIN_IDLE_BREATH_COOLDOWN:
        log.debug(
            "increase IDLE_BREATH cooldown from %.1fs to %.1fs",
            breath.cooldown,
            MIN_IDLE_BREATH_COOLDOWN,
        )
        breath.cooldown = MIN_IDLE_BREATH_COOLDOWN
    return effects


def _get_effects() -> Dict[str, _Effect]:
    """Возвращает кеш эффектов, загружая манифест один раз.

    Кешируем данные, чтобы между последовательными вызовами сохранялось
    поле ``last_played`` и работал механизм ``cooldown``.  Это предотвращает
    повтор звукового эффекта чаще разрешённого интервала.
    """

    global _EFFECTS
    if _EFFECTS is None:
        _EFFECTS = _load_manifest()
    return _EFFECTS


def _read_wav(path: str) -> tuple[np.ndarray, int]:
    """Возвращает аудиоданные и частоту дискретизации."""
    if np is None:  # pragma: no cover - зависит от внешней зависимости
        raise RuntimeError("numpy is required to load WAV files")
    with wave.open(path, "rb") as wf:
        frames = wf.readframes(wf.getnframes())
        data = np.frombuffer(frames, dtype=np.int16).astype(np.float32)
        data /= 32768.0  # нормализуем в диапазон [-1;1]
        return data, wf.getframerate()


def _caller_name() -> str:
    """Определяет модуль и функцию, инициировавшие запуск звука.

    Используем стек вызовов, чтобы в логах было видно, кто именно
    запросил воспроизведение эффекта.  Это упрощает отладку и поиск
    лишних обращений к звуковому драйверу.
    """

    frame = inspect.currentframe()
    # Поднимаемся на два уровня вверх: _caller_name -> play_effect -> вызвавшая функция
    for _ in range(2):
        if frame is None or frame.f_back is None:
            return "<unknown>"
        frame = frame.f_back
    # Если посредником была наша внутренняя обёртка ``_play_effect``,
    # поднимаемся ещё на один уровень, чтобы увидеть реального инициатора.
    if frame and frame.f_code.co_name == "_play_effect" and frame.f_back:
        frame = frame.f_back
    module = frame.f_globals.get("__name__", "<unknown>")
    return f"{module}.{frame.f_code.co_name}"


def _resolve_effect(key: str) -> Optional[tuple[str, _Effect]]:
    """Подбирает эффект с учётом текущей палитры.

    Сначала ищем вариант ``<PAL>:<KEY>`` в соответствии с выбранной
    политикой настроения, затем падаем обратно на базовый ``<KEY>``.
    Возвращаем пару из итогового ключа и объекта эффекта либо ``None``,
    если подходящий эффект не найден.
    """

    effects = _get_effects()
    palette = _CURRENT_PALETTE.upper() if _CURRENT_PALETTE else ""
    if palette:
        pal_key = f"{palette}:{key}"
        eff = effects.get(pal_key)
        if eff and eff.files:
            return pal_key, eff
    eff = effects.get(key)
    if eff and eff.files:
        return key, eff
    return None


def play_effect(name: str | Emotion) -> None:
    """Воспроизводит одиночный эффект по ключу из манифеста."""

    if sd is None or is_quiet_now():
        log.debug("skip effect %s: quiet=%s", name, is_quiet_now())
        return  # звук недоступен или тихие часы

    # Разрешаем использовать псевдонимы; сначала преобразуем имя в верхний
    # регистр, затем пытаемся найти его в словаре ``_ALIASES``.
    key_obj: Any = name
    if not isinstance(name, Emotion):
        key_obj = str(name).upper()
    key = _ALIASES.get(key_obj, key_obj if isinstance(key_obj, str) else key_obj.name)

    resolved = _resolve_effect(str(key).upper())
    if not resolved:
        return
    eff_key, effect = resolved
    base_key = eff_key.split(":")[-1]

    # Для дыхания используем глобальную блокировку, чтобы разные части
    # приложения не воспроизвели звук почти одновременно.
    lock = _idle_breath_lock if base_key == "IDLE_BREATH" else effect.lock
    with lock:
        now = time.monotonic()
        if base_key == "IDLE_BREATH":
            global _idle_breath_last
            if _idle_breath_last + MIN_IDLE_BREATH_COOLDOWN > now:
                remaining = _idle_breath_last + MIN_IDLE_BREATH_COOLDOWN - now
                log.debug("skip %s due to global cooldown %.2fs", eff_key, remaining)
                return
        cooldown = effect.cooldown
        if base_key == "IDLE_BREATH":
            cooldown = max(cooldown, MIN_IDLE_BREATH_COOLDOWN)
        if effect.last_played + cooldown > now:
            remaining = effect.last_played + cooldown - now
            log.debug("skip %s due to cooldown %.2fs", eff_key, remaining)
            return
        if _GLOBAL_LIMITER and not _GLOBAL_LIMITER.allow():
            log.debug("skip %s due to global rate limit", eff_key)
            return

        file = random.choice(effect.files)
        caller = _caller_name()
        log.info("play %s (%s) by %s x%d", eff_key, file, caller, effect.repeat)
        try:
            data, rate = _read_wav(file)
            volume = 10 ** (effect.gain / 20)
            effect.last_played = now
            if base_key == "IDLE_BREATH":
                _idle_breath_last = now
            # Повторяем звук ``repeat`` раз.  При значении >1 блокируемся до
            # окончания каждого проигрывания, чтобы они не накладывались.
            for i in range(effect.repeat):
                log.debug("start %s → %s [%d/%d]", eff_key, file, i + 1, effect.repeat)
                sd.play(data * volume, rate, blocking=effect.repeat > 1)
                log.debug("end %s [%d/%d]", eff_key, i + 1, effect.repeat)
        except Exception:  # pragma: no cover
            log.exception("sound playback failed")


class EmotionSoundDriver:
    """Воспроизводит звуковые файлы при смене эмоции."""

    def __init__(self) -> None:
        self.log = configure_logging("emotion.sound")
        # Используем общий кеш эффектов, чтобы делиться информацией о
        # времени последнего воспроизведения между экземплярами драйвера.
        self._effects = _get_effects()
        self._current: Emotion = Emotion.NEUTRAL
        # Флаг присутствия пользователя перед камерой.  Пока человек в
        # кадре, «вздыхать» не следует, чтобы не создавать впечатление,
        # что ассистент устал от общения.
        self._present: bool = False
        core_events.subscribe("emotion_changed", self._on_emotion_changed)
        core_events.subscribe("presence.update", self._on_presence_update)
        # Таймер, планирующий редкое воспроизведение "дыхания".  Он
        # перезапускается при каждом изменении эмоции или флага присутствия,
        # чтобы звук не звучал, пока пользователь рядом.
        self._breath_timer: Optional[threading.Timer] = None
        self._schedule_idle_breath()

    def _schedule_idle_breath(self) -> None:
        """Переустанавливает таймер фонового дыхания.

        Звук планируется только при нейтральной эмоции и отсутствии
        пользователя перед камерой.  При любых изменениях условия таймер
        отменяется и запускается вновь, чтобы исключить лишние воспроизведения.
        """

        if self._breath_timer:
            self._breath_timer.cancel()
            self._breath_timer = None

        if self._current is not Emotion.NEUTRAL or self._present:
            self.log.debug(
                "idle breath not scheduled: emotion=%s present=%s",
                self._current,
                self._present,
            )
            return

        resolved = _resolve_effect("IDLE_BREATH")
        effect = resolved[1] if resolved else None
        base = MIN_IDLE_BREATH_COOLDOWN
        if effect:
            base = max(effect.cooldown, MIN_IDLE_BREATH_COOLDOWN)
        delay = random.uniform(base, base * 2)
        self.log.debug("idle breath scheduled in %.1fs", delay)
        self._breath_timer = threading.Timer(delay, self._on_idle_breath_timer)
        self._breath_timer.daemon = True
        self._breath_timer.start()

    def _on_idle_breath_timer(self) -> None:
        """Колбэк таймера: воспроизводит дыхание и планирует следующее."""

        self._breath_timer = None
        if self._current is Emotion.NEUTRAL and not self._present:
            self.play_idle_effect()
        self._schedule_idle_breath()

    def _play_effect(self, name: str) -> None:
        """Обёртка над :func:`play_effect` для подмены в тестах."""
        play_effect(name)

    def play_idle_effect(self) -> None:
        """Явно воспроизводит короткое дыхание (используется в тестах)."""
        if self._present:
            # Пользователь в кадре → пропускаем эффект, чтобы не
            # провоцировать нежелательные вздохи.
            self.log.debug("skip idle breath: user present")
            return
        # `_play_effect` дополнительно проверяет глобальный таймер и не
        # допускает повтор чаще одного раза в 15 минут.
        self._play_effect("IDLE_BREATH")

    def _on_presence_update(self, event: core_events.Event) -> None:
        """Обновление флага присутствия пользователя."""
        self._present = bool(event.attrs.get("present"))
        self.log.debug("presence %s", "present" if self._present else "absent")
        # При появлении пользователя или его уходе перепланируем дыхание.
        self._schedule_idle_breath()

    def _on_emotion_changed(self, event: core_events.Event) -> None:
        if sd is None:
            return  # звук недоступен
        sd.stop()  # оборвать звук предыдущей эмоции
        emotion: Emotion = event.attrs["emotion"]
        self._current = emotion
        key = _ALIASES.get(emotion, emotion.name)
        # Обновляем текущую палитру звуков, если менеджер передал её в событии.
        palette = event.attrs.get("sfx_palette")
        if isinstance(palette, str):
            global _CURRENT_PALETTE
            _CURRENT_PALETTE = palette.upper()
            self.log.debug("set palette %s", _CURRENT_PALETTE)
        self._play_effect(key)
        # Каждая смена эмоции влияет на расписание дыхания.
        self._schedule_idle_breath()
