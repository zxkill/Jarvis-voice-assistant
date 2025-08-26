from __future__ import annotations

import array as _array
import inspect
import os
import re
import time
import wave
import hashlib
from pathlib import Path
from typing import Iterable, List
from concurrent.futures import ThreadPoolExecutor

import keyboard  # type: ignore — requires native build on Win
import numpy as np
import sounddevice as sd  # type: ignore
from piper import PiperVoice  # type: ignore — external lib
from transliterate import translit  # type: ignore
from threading import Event

from core.nlp import numbers_to_words, remove_spaces_in_numbers
from core import events as core_events

# ────────────────────────── 0. LOGGING ────────────────────────────
from core.logging_json import configure_logging

# Настройка базового логирования для всей работы модуля
log = configure_logging("tts.piper")

# ────────────────────────── 1. CONFIG ─────────────────────────────
# Идентификатор голоса Piper (файл <VOICE_ID>.onnx должен существовать)
VOICE_ID: str = "ru_RU-ruslan-medium"  # "ru-RU-irina-medium"  # default voice ID
# Пути, где ищем модель
SEARCH_DIRS: List[str] = ["./models/piper"]
# Максимальный размер чанка текста для озвучивания
MAX_CHARS: int = 180
# Пауза в секундах, добавляемая в конец каждого чанка, чтобы не обрезать фразу
TAIL_PAD_SEC: float = 0.3
# Преднастроенные "эмоции"/тона (громкость, скорость, пауза)
# Преднастроенные параметры для эмоциональной озвучки.
# ``pitch`` и ``speed`` регулируют высоту голоса и скорость воспроизведения,
# ``volume`` отвечает за громкость, а ``pause`` — за длину тишины в конце
# чанка.  Значения подобраны эмпирически и служат отправной точкой для
# дальнейшей настройки пользователем.
TTS_PRESETS = {
    "neutral": {"volume": 1.0, "speed": 1.0, "pitch": 1.0, "pause": TAIL_PAD_SEC},
    "happy": {"volume": 1.2, "speed": 1.2, "pitch": 1.1, "pause": 0.2},
    "sad": {"volume": 0.8, "speed": 0.8, "pitch": 0.9, "pause": 0.5},
}
# Можно включить GPU, если доступно
USE_CUDA: bool = False
# Каталог для хранения WAV-файлов кэша
CACHE_DIR: Path = Path("tts_cache")
# Время жизни одного файла кэша (сутки)
CACHE_TTL: float = 24 * 60 * 60
# Интервал между запусками фоновой очистки
_CACHE_CLEAN_INTERVAL: float = 60 * 60
# Временная отметка последней очистки
_last_cache_cleanup: float = 0.0
_STOP_EVENT: Event = Event()           # сигнал прерывания воспроизведения
is_playing: bool = False               # флаг активного озвучивания

# Отдельный пул потоков для TTS, чтобы воспроизведение не блокировало
# общий executor, используемый распознаванием речи
_EXECUTOR: ThreadPoolExecutor = ThreadPoolExecutor(max_workers=1)

# ────────────────────────── 2. LOAD VOICE ─────────────────────────

def _find_voice() -> str:
    """Возвращает абсолютный путь к модели <VOICE_ID>.onnx или возбуждает ошибку."""
    for base in SEARCH_DIRS:
        model_path = os.path.join(base, f"{VOICE_ID}.onnx")
        if os.path.isfile(model_path):
            return model_path
    raise FileNotFoundError(
        f"{VOICE_ID}.onnx(.json) not found. Expected in: \n  "
        + "\n  ".join(os.path.abspath(p) for p in SEARCH_DIRS)
    )

VOICE: PiperVoice = PiperVoice.load(_find_voice(), use_cuda=USE_CUDA)
SAMPLE_RATE: int = VOICE.config.sample_rate
_SIG_HAS_AUDIO_STREAMING: bool = "audio_streaming" in inspect.signature(
    VOICE.synthesize
).parameters
log.info("Модель голоса '%s' загружена (sr=%d, streaming=%s)", VOICE_ID, SAMPLE_RATE, _SIG_HAS_AUDIO_STREAMING)

# ────────────────────────── 3. HELPERS ────────────────────────────
_SENTENCE_RE = re.compile(r"(?<=[.!?…])\s+")

def _split_by_sentences(text: str, max_len: int = MAX_CHARS) -> Iterable[str]:
    """Разбивает текст на предложения и выдаёт чанки не длиннее *max_len*."""
    sentences = _SENTENCE_RE.split(text)
    chunk: str = ""
    for sent in sentences:
        if not sent:
            continue
        if len(chunk) + len(sent) + 1 <= max_len:
            chunk = f"{chunk} {sent}".strip()
        else:
            if chunk:
                yield chunk
            chunk = sent
    if chunk:
        yield chunk

def _to_int16(arr: np.ndarray) -> np.ndarray:  # noqa: N802 (library helper)
    """Приводит массив к типу int16."""
    if arr.dtype == np.int16:
        return arr
    if arr.dtype.kind == "f":  # float32/64 in [-1..1]
        arr = np.clip(arr * 32767.0, -32768, 32767)
        return arr.astype(np.int16)
    return arr.astype(np.int16, copy=False)

def _chunk_to_ndarray(chunk) -> np.ndarray:
    """Извлекает PCM int16 из любого формата чанка Piper."""
    if isinstance(chunk, np.ndarray):
        return _to_int16(chunk)
    if isinstance(chunk, (bytes, bytearray, memoryview)):
        return np.frombuffer(chunk, dtype=np.int16)
    for name in dir(chunk):
        if name.startswith("_"):
            continue
        try:
            val = getattr(chunk, name)
        except Exception:
            continue
        if isinstance(val, _array.array):
            return np.frombuffer(val, dtype=np.int16)
        if isinstance(val, np.ndarray):
            return _to_int16(val)
        if isinstance(val, (bytes, bytearray, memoryview)):
            return np.frombuffer(val, dtype=np.int16)
        if isinstance(val, (list, tuple)) and val and isinstance(val[0], (int, float)):
            return np.asarray(val, dtype=np.int16)
    try:
        return np.asarray(list(chunk), dtype=np.int16)
    except Exception:
        return np.zeros(0, dtype=np.int16)

def _synthesize(text: str) -> np.ndarray:
    """Синтезирует аудио для заданного текста независимо от версии Piper."""
    if not text:
        return np.zeros(0, dtype=np.int16)

    if _SIG_HAS_AUDIO_STREAMING:  # piper‑tts ≥ 1.3
        return _to_int16(VOICE.synthesize(text, audio_streaming=False))

    # piper‑tts 1.2.x — stream, need to concat
    frames = (_chunk_to_ndarray(c) for c in VOICE.synthesize(text))
    return np.concatenate(list(frames))

def _ndarray_to_float32(audio: np.ndarray) -> np.ndarray:
    return audio.astype(np.float32, copy=False) / 32767.0 if audio.size else audio


def _apply_pitch(audio: np.ndarray, pitch: float) -> np.ndarray:
    """Простейшее изменение высоты голоса путём ресемплинга массива.

    Такой подход меняет и длительность сигнала, но для наших целей
    достаточно, поскольку эффект требуется лишь для передачи общего
    настроения (радость, грусть и т.п.).  При ``pitch`` == 1.0 массив
    возвращается без изменений.
    """
    if pitch == 1.0 or audio.size == 0:
        return audio

    # Формируем массив индексов с учётом коэффициента ``pitch`` и
    # выбираем соответствующие сэмплы.  При ``pitch`` > 1 голос становится
    # выше и короче, при ``pitch`` < 1 — ниже и длиннее.
    idx = np.round(np.arange(0, audio.size, 1 / pitch)).astype(int)
    idx = idx[idx < audio.size]
    return audio[idx]


def _cache_path(text: str) -> Path:
    """Возвращает путь к файлу в кэше для заданного текста.

    Раскладываем файлы по подпапкам по первым четырём символам хэша, чтобы
    в одном каталоге не копилось слишком много элементов.
    """
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()
    return CACHE_DIR / digest[:2] / digest[2:4] / f"{digest}.wav"


def _cleanup_cache(now: float) -> None:
    """Удаляет из кэша файлы, к которым не обращались дольше TTL."""
    global _last_cache_cleanup
    if now - _last_cache_cleanup < _CACHE_CLEAN_INTERVAL:
        return
    _last_cache_cleanup = now
    if not CACHE_DIR.exists():
        return
    cutoff = now - CACHE_TTL
    log.debug("Очистка кэша: удаляем файлы старше %.0f сек", CACHE_TTL)
    for root, _, files in os.walk(CACHE_DIR):
        for name in files:
            path = Path(root) / name
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
                    log.debug("Удалён устаревший файл %s", path)
            except FileNotFoundError:
                pass

# ────────────────────────── 4. PUBLIC API ─────────────────────────

def stop_speaking() -> None:
    """Принудительно останавливает текущее воспроизведение (команда «стоп»)."""
    _STOP_EVENT.set()
    try:
        sd.stop()
    except Exception:
        pass

def _save_wav(path: str, pcm_i16: np.ndarray) -> None:
    """Пишет mono-PCM-16 bit в .wav (для отладки)."""
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)              # 16-bit
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(pcm_i16.tobytes())
    log.info("WAV-debug: saved %s (%.2f s)", path, pcm_i16.size / SAMPLE_RATE)


def working_tts(
    text: str,
    *,
    max_chars: int = MAX_CHARS,
    save_wav: str | None = None,        # ← новый арг.
    preset: str = "neutral",
    pitch: float | None = None,
    speed: float | None = None,
    emotion: str | None = None,
) -> None:
    """Озвучивает *text*; при *save_wav* пишет итоговый WAV-файл.

    Дополнительные параметры позволяют управлять эмоциональной окраской
    речи:

    * ``pitch``  — коэффициент изменения высоты голоса (1.0 по умолчанию);
    * ``speed``  — множитель скорости воспроизведения;
    * ``emotion`` — название пресета из :data:`TTS_PRESETS`.
    """
    global is_playing
    is_playing = True
    core_events.publish(
        core_events.Event(
            kind="speech.synthesis_started",
            attrs={"text": text, "emotion": emotion or preset},
        )
    )
    _STOP_EVENT.clear()
    # Приводим исходный текст к удобному для синтеза виду:
    # 1) числа → слова, 2) убираем пробелы в числах, 3) транслитерация
    norm = translit(remove_spaces_in_numbers(numbers_to_words(text)), "ru")
    log.info("Озвучиваем строку длиной %d символов (preset=%s)", len(norm), preset)
    cfg = TTS_PRESETS.get(emotion or preset, TTS_PRESETS["neutral"])
    vol = cfg["volume"]
    speed = speed if speed is not None else cfg["speed"]
    pitch = pitch if pitch is not None else cfg["pitch"]
    pause = cfg["pause"]

    log.info(
        "Эмоция=%s | pitch=%.2f | speed=%.2f", (emotion or preset), pitch, speed
    )

    now = time.time()
    # Периодическая очистка устаревшего кэша
    _cleanup_cache(now)

    playback_parts: list[np.ndarray] = []

    for i, chunk in enumerate(_split_by_sentences(norm, max_chars), 1):
        if _STOP_EVENT.is_set():
            break
        now = time.time()
        cache_file = _cache_path(chunk)
        pcm_i16_pad: np.ndarray | None = None
        t_gen = 0.0

        # --- 4.1. Попытка взять аудио из кэша ---
        if cache_file.exists():
            if now - cache_file.stat().st_mtime <= CACHE_TTL:
                # Успешный хит: обновляем mtime, чтобы продлить жизнь файла
                os.utime(cache_file, None)
                log.debug("Чанк %d найден в кэше: %s", i, cache_file)
                # wave.open в Python 3.10 не принимает объект Path напрямую,
                # поэтому приводим путь к строке
                with wave.open(str(cache_file), "rb") as wf:
                    pcm_i16_pad = np.frombuffer(
                        wf.readframes(wf.getnframes()), dtype=np.int16
                    )
            else:
                # Файл есть, но устарел — удаляем
                log.debug("Чанк %d устарел в кэше, удаляем %s", i, cache_file)
                try:
                    cache_file.unlink()
                except FileNotFoundError:
                    pass

        # --- 4.2. Кэш не сработал, запускаем синтез ---
        if pcm_i16_pad is None:
            log.debug("Чанк %d отсутствует в кэше, запускаем синтез", i)
            t0 = time.perf_counter()
            pcm_i16 = _synthesize(chunk)  # int16 от Piper
            t_gen = time.perf_counter() - t0
            if not pcm_i16.size:
                log.warning("Чанк %d: пустой аудио-результат", i)
                continue

            # Добавляем тишину в хвосте, чтобы не обрывалась последняя буква
            tail = np.zeros(int(SAMPLE_RATE * pause), np.int16)
            pcm_i16_pad = np.concatenate([pcm_i16, tail])
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            _save_wav(str(cache_file), pcm_i16_pad)
            log.debug("Чанк %d сохранён в кэш %s", i, cache_file)

        # Применяем изменение высоты голоса согласно коэффициенту ``pitch``
        pcm_i16_pad = _apply_pitch(pcm_i16_pad, pitch)
        playback_parts.append(pcm_i16_pad)
        audio_f32 = pcm_i16_pad.astype(np.float32) / 32767.0
        if vol != 1.0:
            audio_f32 = np.clip(audio_f32 * vol, -1.0, 1.0)

        # Запускаем воспроизведение. ``sd.wait`` занимает GIL,
        # и поэтому не даёт реагировать на слово «стоп».
        # Вместо этого ожидаем завершения в простом цикле с небольшим сном,
        # что даёт возможность другим потокам читать микрофон.
        t1 = time.perf_counter()
        duration = audio_f32.size / (SAMPLE_RATE * speed)
        sd.play(audio_f32, int(SAMPLE_RATE * speed), blocking=False)
        end_time = t1 + duration
        while time.perf_counter() < end_time:
            if _STOP_EVENT.is_set():
                break
            time.sleep(0.05)
        sd.stop()
        t_play = time.perf_counter() - t1

        rms = float(np.sqrt(np.mean(audio_f32 ** 2)))
        log.info(
            "part %2d | gen %.2fs | play %.2fs | len %6d | rms %.3f",
            i, t_gen, t_play, pcm_i16_pad.size, rms,
        )
        if _STOP_EVENT.is_set():
            break

    full_audio = np.concatenate(playback_parts) if playback_parts else np.zeros(0, np.int16)
    if save_wav:
        _save_wav(save_wav, full_audio)
    total_duration = full_audio.size / (SAMPLE_RATE * speed) if speed else 0.0
    log.info("Озвучивание завершено: длительность %.2f с", total_duration)
    is_playing = False
    core_events.publish(core_events.Event(kind="speech.synthesis_finished"))
    _STOP_EVENT.clear()
    sd.stop()

# ────────────────────────── 5. ASYNC WRAPPER ─────────────────────

async def speak_async(
    text: str,
    *,
    preset: str = "neutral",
    pitch: float | None = None,
    speed: float | None = None,
    emotion: str | None = None,
    loop: asyncio.AbstractEventLoop | None = None,
) -> None:
    """Неблокирующая озвучка: `working_tts` выполняется в пуле потоков."""
    from core.request_source import get_request_source
    if get_request_source() == "telegram":
        try:
            import importlib
            from core.metrics import inc_metric

            tg = importlib.import_module("notifiers.telegram")
            tg.send(text)
            log.info("telegram reply text=%r", text)
            inc_metric("telegram.outgoing")
        except Exception as exc:  # pragma: no cover - защищаемся от сетевых ошибок
            log.warning("telegram reply failed: %s", exc)
        return

    import asyncio  # локальный импорт, чтобы не тянуть asyncio в синхронный контекст
    from functools import partial

    loop = loop or asyncio.get_running_loop()
    func = partial(working_tts, text, preset=preset, pitch=pitch, speed=speed, emotion=emotion)
    # Используем отдельный executor, чтобы не блокировать поток
    # чтения с микрофона, работающий через asyncio.to_thread
    await loop.run_in_executor(_EXECUTOR, func)

# ────────────────────────── 6. CLI TEST ──────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Test Piper‑TTS playback")
    parser.add_argument("text", nargs="*", help="Text to speak")
    parser.add_argument("--chars", "-c", type=int, default=MAX_CHARS, help="Chunk size")
    args = parser.parse_args()

    if args.text:
        working_tts(" ".join(args.text), max_chars=args.chars)
    else:
        working_tts("Привет! Я Джарвис, ваш голосовой ассистент.")
