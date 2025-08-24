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
from typing import Dict, List

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

try:  # ``sounddevice`` может быть недоступен в среде тестирования
    import sounddevice as sd  # type: ignore
except Exception:  # pragma: no cover
    sd = None  # type: ignore


MANIFEST_PATH = Path(__file__).resolve().parent.parent / "audio" / "sfx_manifest.yaml"


# соответствие некоторых эмоций ключам в манифесте
_ALIASES: Dict[Emotion, str] = {
    Emotion.NEUTRAL: "IDLE",
    Emotion.SLEEPY: "SLEEP",
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


class EmotionSoundDriver:
    """Воспроизводит звуковые файлы при смене эмоции."""

    def __init__(self) -> None:
        self.log = configure_logging("emotion.sound")
        self._effects = _load_manifest()
        core_events.subscribe("emotion_changed", self._on_emotion_changed)

    def _on_emotion_changed(self, event: core_events.Event) -> None:
        if sd is None:
            return  # звук недоступен
        emotion: Emotion = event.attrs["emotion"]
        key = _ALIASES.get(emotion, emotion.name)
        effect = self._effects.get(key)
        if not effect or not effect.files:
            return
        now = time.monotonic()
        if effect.last_played + effect.cooldown > now:
            return
        file = random.choice(effect.files)
        try:
            data, rate = _read_wav(file)
            volume = 10 ** (effect.gain / 20)
            sd.play(data * volume, rate, blocking=False)
            effect.last_played = now
        except Exception:  # pragma: no cover
            self.log.exception("sound playback failed")
