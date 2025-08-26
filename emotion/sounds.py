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
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Any

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

try:  # ``sounddevice`` может быть недоступен в среде тестирования
    import sounddevice as sd  # type: ignore
except Exception:  # pragma: no cover
    sd = None  # type: ignore


MANIFEST_PATH = Path(__file__).resolve().parent.parent / "audio" / "sfx_manifest.yaml"
log = configure_logging("emotion.sound")


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

@dataclass
class _Effect:
    files: List[str]
    gain: float
    cooldown: float
    last_played: float = 0.0


def _load_manifest() -> Dict[str, _Effect]:
    if not MANIFEST_PATH.exists() or yaml is None:
        return {}
    try:
        data = yaml.safe_load(MANIFEST_PATH.read_text("utf-8")) or {}
    except Exception:  # pragma: no cover - повреждённый YAML
        return {}

    effects: Dict[str, _Effect] = {}
    for name, cfg in data.items():
        if not isinstance(cfg, dict):
            continue
        files = [str(f) for f in cfg.get("files", [])]
        gain = float(cfg.get("gain_db", 0))
        cooldown = float(cfg.get("cooldown_ms", 0)) / 1000.0
        if isinstance(name, bool):
            key = "YES" if name else "NO"
        else:
            key = str(name).upper()
        effects[key] = _Effect(files=files, gain=gain, cooldown=cooldown)
    return effects


def _read_wav(path: str) -> tuple[np.ndarray, int]:
    """Возвращает аудиоданные и частоту дискретизации."""
    if np is None:  # pragma: no cover - зависит от внешней зависимости
        raise RuntimeError("numpy is required to load WAV files")
    with wave.open(path, "rb") as wf:
        frames = wf.readframes(wf.getnframes())
        data = np.frombuffer(frames, dtype=np.int16).astype(np.float32)
        data /= 32768.0  # нормализуем в диапазон [-1;1]
        return data, wf.getframerate()


def play_effect(name: str | Emotion) -> None:
    """Воспроизводит одиночный эффект по ключу из манифеста."""
    if sd is None or is_quiet_now():
        log.debug("skip effect %s: quiet=%s", name, is_quiet_now())
        return  # звук недоступен или тихие часы
    effects = _load_manifest()
    # разрешаем использовать псевдонимы; сначала преобразуем имя в верхний
    # регистр, затем пытаемся найти его в словаре ``_ALIASES``
    key_obj: Any = name
    if not isinstance(name, Emotion):
        key_obj = str(name).upper()
    key = _ALIASES.get(key_obj, key_obj if isinstance(key_obj, str) else key_obj.name)
    effect = effects.get(str(key).upper())
    if not effect or not effect.files:
        return
    file = random.choice(effect.files)
    try:
        data, rate = _read_wav(file)
        volume = 10 ** (effect.gain / 20)
        log.debug("start %s → %s", key, file)
        sd.play(data * volume, rate, blocking=False)
        log.debug("end %s", key)
    except Exception:  # pragma: no cover
        log.exception("sound playback failed")


class EmotionSoundDriver:
    """Воспроизводит звуковые файлы при смене эмоции."""

    def __init__(self) -> None:
        self.log = configure_logging("emotion.sound")
        # Предзагружаем манифест, чтобы иметь доступ к cooldown и спискам
        # файлов без повторного чтения с диска
        self._effects = _load_manifest()
        self._current: Emotion = Emotion.NEUTRAL
        # Флаг присутствия пользователя перед камерой.  Пока человек в
        # кадре, «вздыхать» не следует, чтобы не создавать впечатление,
        # что ассистент устал от общения.
        self._present: bool = False
        core_events.subscribe("emotion_changed", self._on_emotion_changed)
        core_events.subscribe("presence.update", self._on_presence_update)
        # Фоновый поток воспроизводит короткие «дыхания» во время простоя.
        # Поток демонический, чтобы не блокировать завершение приложения.
        import threading

        threading.Thread(target=self._idle_loop, daemon=True).start()

    def _idle_loop(self) -> None:
        """Фоновый цикл, периодически запускающий короткое дыхание."""
        effect = self._effects.get("IDLE_BREATH")
        if not effect:
            return  # в манифесте нет описания — работать нечему
        while True:
            # Ждём случайную паузу, чтобы звуки не звучали по расписанию
            delay = random.uniform(effect.cooldown, effect.cooldown * 2 or 1.0)
            time.sleep(delay)
            # Воспроизводим «дыхание» только если никого нет в кадре и
            # текущая эмоция нейтральна.  Иначе пользователю будет казаться,
            # что ассистент вздыхает при виде собеседника.
            if self._current is Emotion.NEUTRAL and not self._present:
                self.play_idle_effect()

    def _play_effect(self, name: str) -> None:
        """Воспроизводит эффект из заранее загруженного словаря."""
        if sd is None or is_quiet_now():
            self.log.debug("skip %s: quiet=%s", name, is_quiet_now())
            return
        effect = self._effects.get(name)
        if not effect or not effect.files:
            return
        now = time.monotonic()
        if effect.last_played + effect.cooldown > now:
            remaining = effect.last_played + effect.cooldown - now
            self.log.debug("skip %s due to cooldown %.2fs", name, remaining)
            return
        file = random.choice(effect.files)
        self.log.debug("start %s → %s", name, file)
        try:
            data, rate = _read_wav(file)
            volume = 10 ** (effect.gain / 20)
            sd.play(data * volume, rate, blocking=False)
            effect.last_played = now
            self.log.debug("end %s", name)
        except Exception:  # pragma: no cover
            self.log.exception("sound playback failed")

    def play_idle_effect(self) -> None:
        """Явно воспроизводит короткое дыхание (используется в тестах)."""
        if self._present:
            # Пользователь в кадре → пропускаем эффект, чтобы не
            # провоцировать нежелательные вздохи.
            self.log.debug("skip idle breath: user present")
            return
        self._play_effect("IDLE_BREATH")

    def _on_presence_update(self, event: core_events.Event) -> None:
        """Обновление флага присутствия пользователя."""
        self._present = bool(event.attrs.get("present"))
        self.log.debug("presence %s", "present" if self._present else "absent")

    def _on_emotion_changed(self, event: core_events.Event) -> None:
        if sd is None:
            return  # звук недоступен
        sd.stop()  # оборвать звук предыдущей эмоции
        emotion: Emotion = event.attrs["emotion"]
        self._current = emotion
        key = _ALIASES.get(emotion, emotion.name)
        self._play_effect(key)
