"""Звуковые эмоции Jarvis.

Локально эффект можно слушать в родном виде, но на ESP32/MAX98357A
электронные SFX в 44.1 kHz часто звучат резко и неприятно: маленький
динамик подчёркивает верх, а ESP32 не всегда идеально держит 44.1 kHz.

Поэтому для робота эффекты мягко готовятся отдельно:
- mono PCM16;
- единая частота 22050 Гц, близкая к TTS;
- лёгкий low-pass перед ресемплом;
- короткий fade-in/fade-out против щелчков;
- только защита от клиппинга, без агрессивной нормализации.
"""

from __future__ import annotations

import inspect
import io
import random
import shutil
import subprocess
import threading
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import Any, Callable, Dict, List, Optional

try:
    import numpy as np  # type: ignore
except Exception:  # pragma: no cover
    np = None  # type: ignore

try:
    import yaml  # type: ignore
except Exception:  # pragma: no cover
    yaml = None  # type: ignore

try:
    import sounddevice as sd  # type: ignore
except Exception:  # pragma: no cover
    sd = None  # type: ignore

from core import events as core_events
from core.logging_json import configure_logging
from core.quiet import is_quiet_now
from emotion.state import Emotion
from utils.rate_limiter import RateLimiter

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MANIFEST_PATH = PROJECT_ROOT / "audio" / "sfx_manifest.yaml"
# Роботный тракт: ESP32 + MAX98357A + маленький динамик.
# Для него лучше не гонять SFX в 44.1 kHz, а привести их к частоте,
# близкой к голосу Piper. Так меньше переключений I2S и меньше резкого верха.
ROBOT_EFFECT_SAMPLE_RATE = 22050
ROBOT_EFFECT_LOW_PASS_HZ = 8500.0
ROBOT_EFFECT_FADE_MS = 6.0
ROBOT_EFFECT_TARGET_PEAK = 0.32   # примерно -10 dBFS: слышно, но без визга
ROBOT_EFFECT_MAX_AUTO_GAIN = 4.0  # максимум +12 dB после фильтра

# Основная громкость всё ещё управляется gain_db в manifest.
# Небольшая автоподстройка ниже нужна только потому, что low-pass может
# сильно ослабить ультравысокочастотные эффекты.
ROBOT_EXTRA_GAIN_DB = 0.0
SAFE_PEAK = 0.92
MIN_IDLE_BREATH_COOLDOWN = 15 * 60

log = configure_logging("emotion.sound")


@dataclass
class _Effect:
    files: List[str]
    gain: float
    cooldown: float
    repeat: int = 1
    last_played: float = 0.0
    lock: Lock = field(default_factory=Lock, repr=False)


_EFFECTS: Dict[str, _Effect] | None = None
_CURRENT_PALETTE: str = ""
_GLOBAL_LIMITER: RateLimiter | None = None
_LOCAL_PLAYBACK_ENABLED: bool = True
_STREAM_LISTENERS: List[Callable[..., None]] = []
_STREAM_LOCK: Lock = Lock()
_idle_breath_last: float = 0.0
_idle_breath_lock = Lock()

_ALIASES: Dict[Any, str] = {
    Emotion.NEUTRAL: "IDLE",
    "IDLE_BREATH": "IDLE_BREATH",
    "WAKE": "WAKE",
    "YAWN": "YAWN",
    "SIGH": "SIGH",
}


def set_local_playback_enabled(enabled: bool) -> None:
    """Включает или отключает локальный вывод через колонки компьютера."""

    global _LOCAL_PLAYBACK_ENABLED
    _LOCAL_PLAYBACK_ENABLED = bool(enabled)
    log.info(
        "Локальное воспроизведение звуковых эффектов %s",
        "включено" if _LOCAL_PLAYBACK_ENABLED else "отключено",
    )


def register_stream_listener(listener: Callable[..., None]) -> None:
    """Добавляет подписчика, которому нужно отправлять PCM эффектов."""

    with _STREAM_LOCK:
        if listener not in _STREAM_LISTENERS:
            _STREAM_LISTENERS.append(listener)
    log.info("Добавлен слушатель потокового вывода эффектов: %s", listener)


def unregister_stream_listener(listener: Callable[..., None]) -> None:
    """Удаляет подписчика PCM эффектов."""

    with _STREAM_LOCK:
        try:
            _STREAM_LISTENERS.remove(listener)
        except ValueError:
            return
    log.info("Удалён слушатель потокового вывода эффектов: %s", listener)


def _notify_stream_listeners(
    pcm: bytes,
    sample_rate: int,
    *,
    name: str,
    file: str,
    repeat_index: int,
    repeat_total: int,
    volume: float,
    channels: int = 1,
) -> None:
    with _STREAM_LOCK:
        listeners = list(_STREAM_LISTENERS)
    for listener in listeners:
        try:
            listener(
                pcm,
                sample_rate,
                name=name,
                source_file=file,
                repeat_index=repeat_index,
                repeat_total=repeat_total,
                volume=volume,
                channels=channels,
            )
        except Exception:
            log.exception("Ошибка уведомления слушателя эффектов %s", listener)


def _load_manifest() -> Dict[str, _Effect]:
    if not MANIFEST_PATH.exists() or yaml is None:
        return {}
    try:
        data = yaml.safe_load(MANIFEST_PATH.read_text("utf-8")) or {}
    except Exception:
        log.exception("Не удалось прочитать манифест эффектов")
        return {}

    global _GLOBAL_LIMITER
    rate_ms = float(data.pop("global_rate_limit_ms", 0) or 0)
    if rate_ms > 0:
        _GLOBAL_LIMITER = RateLimiter(1, rate_ms / 1000.0)

    effects: Dict[str, _Effect] = {}
    for name, cfg in data.items():
        if not isinstance(cfg, dict):
            continue
        key = "YES" if name is True else "NO" if name is False else str(name).upper()
        effects[key] = _Effect(
            files=[str(f) for f in cfg.get("files", [])],
            gain=float(cfg.get("gain_db", 0) or 0),
            cooldown=float(cfg.get("cooldown_ms", 0) or 0) / 1000.0,
            repeat=max(1, int(cfg.get("repeat", 1) or 1)),
        )

    breath = effects.get("IDLE_BREATH")
    if breath and breath.cooldown < MIN_IDLE_BREATH_COOLDOWN:
        breath.cooldown = MIN_IDLE_BREATH_COOLDOWN
    return effects


def _get_effects() -> Dict[str, _Effect]:
    global _EFFECTS
    if _EFFECTS is None:
        _EFFECTS = _load_manifest()
    return _EFFECTS


def _resolve_audio_path(path: str) -> Path:
    """Возвращает абсолютный путь к файлу эффекта.

    В манифесте пути записаны относительно корня проекта. Если Jarvis
    запущен из другой рабочей директории, обычный wave.open('audio/...')
    падает FileNotFoundError. Поэтому всегда резолвим путь от PROJECT_ROOT.
    """

    p = Path(path)
    if p.is_absolute():
        return p
    return (PROJECT_ROOT / p).resolve()


def _decode_with_ffmpeg(path: str) -> tuple[Any, int]:
    """Читает mp3/ogg через ffmpeg, сохраняя родную частоту файла."""

    if np is None:
        raise RuntimeError("numpy is required")
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg не найден; сконвертируйте эффект в WAV")

    resolved = _resolve_audio_path(path)
    if not resolved.exists():
        raise FileNotFoundError(str(resolved))

    cmd = [
        "ffmpeg",
        "-v",
        "error",
        "-i",
        str(resolved),
        "-acodec",
        "pcm_s16le",
        "-ac",
        "1",
        "-f",
        "wav",
        "-",
    ]
    raw_wav = subprocess.check_output(cmd)
    return _read_wav_mono_from_fileobj(io.BytesIO(raw_wav))


def _read_wav_mono_from_fileobj(fileobj: Any) -> tuple[Any, int]:
    """Читает WAV/file-like и возвращает float32 mono [-1;1]."""

    if np is None:
        raise RuntimeError("numpy is required")

    with wave.open(fileobj, "rb") as wf:
        channels = wf.getnchannels()
        sample_width = wf.getsampwidth()
        rate = wf.getframerate()
        frames = wf.readframes(wf.getnframes())

    if sample_width == 1:
        data = (np.frombuffer(frames, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    elif sample_width == 2:
        data = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
    elif sample_width == 4:
        data = np.frombuffer(frames, dtype="<i4").astype(np.float32) / 2147483648.0
    else:
        raise RuntimeError(f"unsupported wav sample width: {sample_width}")

    if channels > 1:
        usable = (data.size // channels) * channels
        data = data[:usable].reshape(-1, channels).mean(axis=1)
    return data.astype(np.float32, copy=False), rate


def _read_wav_mono(path: str) -> tuple[Any, int]:
    """Читает WAV и возвращает float32 mono [-1;1]."""

    resolved = _resolve_audio_path(path)
    if not resolved.exists():
        raise FileNotFoundError(str(resolved))
    return _read_wav_mono_from_fileobj(str(resolved))


def _float32_to_pcm16(samples: Any) -> bytes:
    clipped = np.clip(samples, -1.0, 1.0)
    return (clipped * 32767.0).astype("<i2").tobytes()


def _apply_short_fades(samples: Any, rate: int) -> Any:
    """Убирает щелчки на старте/конце коротким плавным входом/выходом."""

    if np is None or samples.size == 0 or rate <= 0:
        return samples
    fade_len = int(rate * ROBOT_EFFECT_FADE_MS / 1000.0)
    fade_len = max(0, min(fade_len, samples.size // 2))
    if fade_len <= 1:
        return samples
    out = samples.astype(np.float32, copy=True)
    fade_in = np.linspace(0.0, 1.0, fade_len, dtype=np.float32)
    fade_out = np.linspace(1.0, 0.0, fade_len, dtype=np.float32)
    out[:fade_len] *= fade_in
    out[-fade_len:] *= fade_out
    return out


def _lowpass_fir(samples: Any, rate: int, cutoff_hz: float) -> Any:
    """Простой FIR low-pass перед понижением частоты.

    Это важнее обычного ресемпла: без low-pass высокие частоты sci-fi SFX
    дают aliasing и превращаются в неприятный цифровой писк.
    """

    if np is None or samples.size == 0 or rate <= 0:
        return samples
    nyquist = rate / 2.0
    cutoff = min(float(cutoff_hz), nyquist * 0.90)
    if cutoff <= 0 or cutoff >= nyquist * 0.90:
        return samples.astype(np.float32, copy=False)

    # 101 tap — лёгкий фильтр, достаточно хороший для коротких эффектов
    # и не слишком тяжёлый для Raspberry Pi/ноутбука.
    taps_count = 101
    n = np.arange(taps_count, dtype=np.float32) - (taps_count - 1) / 2.0
    fc = cutoff / rate
    taps = 2.0 * fc * np.sinc(2.0 * fc * n)
    taps *= np.hamming(taps_count).astype(np.float32)
    taps /= np.sum(taps)
    filtered = np.convolve(samples.astype(np.float32, copy=False), taps, mode="same")
    return filtered.astype(np.float32, copy=False)


def _resample_linear(samples: Any, src_rate: int, dst_rate: int) -> Any:
    """Лёгкий ресемпл без внешних зависимостей."""

    if np is None or samples.size == 0 or src_rate <= 0 or dst_rate <= 0:
        return samples
    if src_rate == dst_rate:
        return samples.astype(np.float32, copy=False)

    # Частый случай 44100 -> 22050: после low-pass можно аккуратно
    # проредить каждый второй сэмпл без лишней интерполяции.
    if src_rate == dst_rate * 2:
        return samples[::2].astype(np.float32, copy=False)

    dst_len = max(1, int(round(samples.size * float(dst_rate) / float(src_rate))))
    src_pos = np.arange(samples.size, dtype=np.float32)
    dst_pos = np.linspace(0, samples.size - 1, dst_len, dtype=np.float32)
    return np.interp(dst_pos, src_pos, samples).astype(np.float32, copy=False)


def _prepare_robot_effect_samples(samples: Any, rate: int) -> tuple[Any, int]:
    """Делает версию эффекта, комфортную для ESP32/MAX98357A."""

    if np is None:
        raise RuntimeError("numpy is required")
    if samples.size == 0:
        return samples.astype(np.float32, copy=False), rate

    target_rate = ROBOT_EFFECT_SAMPLE_RATE
    # Срезаем верх перед ресемплом. Для приложенного эффекта почти вся энергия
    # была выше 8 кГц, на маленьком динамике это звучит особенно резко.
    cutoff = min(ROBOT_EFFECT_LOW_PASS_HZ, target_rate * 0.45)
    prepared = _lowpass_fir(samples, rate, cutoff)
    prepared = _resample_linear(prepared, rate, target_rate)
    prepared = _apply_short_fades(prepared, target_rate)

    peak = float(np.max(np.abs(prepared))) if prepared.size else 0.0
    if 0.0 < peak < ROBOT_EFFECT_TARGET_PEAK:
        gain = min(ROBOT_EFFECT_TARGET_PEAK / peak, ROBOT_EFFECT_MAX_AUTO_GAIN)
        prepared = prepared * gain
        peak *= gain
    if peak > SAFE_PEAK:
        prepared = prepared * (SAFE_PEAK / peak)
    return np.clip(prepared, -1.0, 1.0).astype(np.float32, copy=False), target_rate


def _prepare_effect_audio(path: str, gain_db: float) -> tuple[Any, bytes, int, float]:
    """Готовит эффект в едином мягком формате для робота.

    Локальное воспроизведение тоже получает эту версию. Так проще
    сравнивать, что именно уйдёт на ESP32.
    """

    if np is None:
        raise RuntimeError("numpy is required")

    suffix = Path(path).suffix.lower()
    if suffix == ".wav":
        samples, rate = _read_wav_mono(path)
    else:
        samples, rate = _decode_with_ffmpeg(path)

    gain = 10 ** ((gain_db + ROBOT_EXTRA_GAIN_DB) / 20.0)
    local_samples = samples * gain

    local_peak = float(np.max(np.abs(local_samples))) if local_samples.size else 0.0
    if local_peak > SAFE_PEAK:
        # Только защита от клиппинга. Тихие эффекты не подтягиваем вверх.
        local_samples = local_samples * (SAFE_PEAK / local_peak)
    local_samples = np.clip(local_samples, -1.0, 1.0).astype(np.float32, copy=False)

    robot_samples, robot_rate = _prepare_robot_effect_samples(local_samples, rate)
    pcm = _float32_to_pcm16(robot_samples)
    duration = robot_samples.size / robot_rate if robot_rate else 0.0
    return robot_samples, pcm, robot_rate, duration


def _caller_name() -> str:
    frame = inspect.currentframe()
    for _ in range(2):
        if frame is None or frame.f_back is None:
            return "<unknown>"
        frame = frame.f_back
    if frame and frame.f_code.co_name == "_play_effect" and frame.f_back:
        frame = frame.f_back
    module = frame.f_globals.get("__name__", "<unknown>")
    return f"{module}.{frame.f_code.co_name}"


def _resolve_effect(key: str) -> Optional[tuple[str, _Effect]]:
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
    """Воспроизводит одиночный эффект из манифеста."""

    if is_quiet_now():
        log.debug("skip effect %s: quiet hours", name)
        return

    with _STREAM_LOCK:
        has_listeners = bool(_STREAM_LISTENERS)
    local_available = _LOCAL_PLAYBACK_ENABLED and sd is not None
    if not local_available and not has_listeners:
        log.debug("skip effect %s: нет локального вывода и подписчиков", name)
        return

    key_obj: Any = name
    if not isinstance(name, Emotion):
        key_obj = str(name).upper()
    key = _ALIASES.get(key_obj, key_obj if isinstance(key_obj, str) else key_obj.name)

    resolved = _resolve_effect(str(key).upper())
    if not resolved:
        return
    eff_key, effect = resolved
    base_key = eff_key.split(":")[-1]

    lock = _idle_breath_lock if base_key == "IDLE_BREATH" else effect.lock
    with lock:
        now = time.monotonic()
        if base_key == "IDLE_BREATH":
            global _idle_breath_last
            if _idle_breath_last + MIN_IDLE_BREATH_COOLDOWN > now:
                return
        cooldown = max(effect.cooldown, MIN_IDLE_BREATH_COOLDOWN) if base_key == "IDLE_BREATH" else effect.cooldown
        if effect.last_played + cooldown > now:
            return
        files = list(effect.files)
        random.shuffle(files)

        prepared: tuple[str, Any, bytes, int, float] | None = None
        for file in files:
            try:
                samples, pcm_bytes, rate, duration_sec = _prepare_effect_audio(file, effect.gain)
                prepared = (file, samples, pcm_bytes, rate, duration_sec)
                break
            except FileNotFoundError as exc:
                # Файл из манифеста отсутствует или путь был относительным к другой
                # рабочей директории. Не роняем озвучку — пробуем следующий эффект.
                log.warning("effect file not found: %s", exc)
            except Exception:
                log.exception("sound playback failed for %s", file)

        if prepared is None:
            log.warning("skip %s: нет доступных файлов эффекта", eff_key)
            return

        if _GLOBAL_LIMITER and not _GLOBAL_LIMITER.allow():
            return

        file, samples, pcm_bytes, rate, duration_sec = prepared
        caller = _caller_name()
        log.info("play %s (%s) by %s x%d", eff_key, file, caller, effect.repeat)
        effect.last_played = now
        if base_key == "IDLE_BREATH":
            _idle_breath_last = now

        # Передаём volume=1.0: gain_db уже применён к PCM.
        for i in range(effect.repeat):
            _notify_stream_listeners(
                pcm_bytes,
                rate,
                name=eff_key,
                file=file,
                repeat_index=i + 1,
                repeat_total=effect.repeat,
                volume=1.0,
                channels=1,
            )
            need_block = effect.repeat > 1
            if local_available and sd is not None:
                sd.play(samples, rate, blocking=need_block)
            elif need_block and duration_sec > 0:
                time.sleep(duration_sec)


class EmotionSoundDriver:
    """Воспроизводит звуковые эффекты при смене эмоции."""

    def __init__(self) -> None:
        self.log = configure_logging("emotion.sound")
        self._effects = _get_effects()
        self._current: Emotion = Emotion.NEUTRAL
        self._present: bool = False
        self._breath_timer: Optional[threading.Timer] = None
        core_events.subscribe("emotion_changed", self._on_emotion_changed)
        core_events.subscribe("presence.update", self._on_presence_update)
        self._schedule_idle_breath()

    def _schedule_idle_breath(self) -> None:
        if self._breath_timer:
            self._breath_timer.cancel()
            self._breath_timer = None
        if self._current is not Emotion.NEUTRAL or self._present:
            return
        resolved = _resolve_effect("IDLE_BREATH")
        effect = resolved[1] if resolved else None
        base = max(effect.cooldown, MIN_IDLE_BREATH_COOLDOWN) if effect else MIN_IDLE_BREATH_COOLDOWN
        delay = random.uniform(base, base * 2)
        self._breath_timer = threading.Timer(delay, self._on_idle_breath_timer)
        self._breath_timer.daemon = True
        self._breath_timer.start()

    def _on_idle_breath_timer(self) -> None:
        self._breath_timer = None
        if self._current is Emotion.NEUTRAL and not self._present:
            self.play_idle_effect()
        self._schedule_idle_breath()

    def _play_effect(self, name: str) -> None:
        play_effect(name)

    def play_idle_effect(self) -> None:
        if self._present:
            return
        self._play_effect("IDLE_BREATH")

    def _on_presence_update(self, event: core_events.Event) -> None:
        self._present = bool(event.attrs.get("present"))
        self._schedule_idle_breath()

    def _on_emotion_changed(self, event: core_events.Event) -> None:
        with _STREAM_LOCK:
            has_listeners = bool(_STREAM_LISTENERS)
        if sd is None and not has_listeners:
            return
        if sd is not None:
            sd.stop()
        emotion: Emotion = event.attrs["emotion"]
        self._current = emotion
        palette = event.attrs.get("sfx_palette")
        if isinstance(palette, str):
            global _CURRENT_PALETTE
            _CURRENT_PALETTE = palette.upper()
        key = _ALIASES.get(emotion, emotion.name)
        self._play_effect(key)
        self._schedule_idle_breath()
