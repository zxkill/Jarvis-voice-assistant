from __future__ import annotations

import array as _array
import inspect
import os
import re
import time
import wave
import hashlib
from pathlib import Path
from typing import Callable, Iterable, List
from concurrent.futures import ThreadPoolExecutor

import keyboard  # type: ignore — requires native build on Win
import numpy as np
import sounddevice as sd  # type: ignore
from piper import PiperVoice  # type: ignore — external lib
from transliterate import translit  # type: ignore
from threading import Event, Lock

from core.nlp import normalize_tts_text
from core import events as core_events

# ────────────────────────── 0. LOGGING ────────────────────────────
from core.logging_json import configure_logging
from utils.audio_resample import resample_pcm16

# Настройка базового логирования для всей работы модуля
log = configure_logging("tts.piper")

# Флаг, управляющий локальным воспроизведением через колонки ноутбука.
# По умолчанию включён, но может быть отключён конфигурацией, если звук
# нужно транслировать исключительно на робота.
_LOCAL_PLAYBACK_ENABLED: bool = True

# Список слушателей, которым нужно передавать синтезированный PCM для
# дальнейшей ретрансляции (например, роботу по WebSocket).
_STREAM_LISTENERS: List[Callable[..., None]] = []
# Блокировка предотвращает гонки между регистрацией слушателей и отправкой.
_STREAM_LOCK: Lock = Lock()


def set_local_playback_enabled(enabled: bool) -> None:
    """Включает или отключает локальное воспроизведение через ``sounddevice``."""

    global _LOCAL_PLAYBACK_ENABLED
    _LOCAL_PLAYBACK_ENABLED = bool(enabled)
    log.info(
        "Локальное воспроизведение %s",
        "включено" if _LOCAL_PLAYBACK_ENABLED else "отключено",
    )


def register_stream_listener(listener: Callable[..., None]) -> None:
    """Добавляет внешний обработчик для передачи PCM роботам или сервисам."""

    with _STREAM_LOCK:
        if listener not in _STREAM_LISTENERS:
            _STREAM_LISTENERS.append(listener)
    log.info("Добавлен слушатель потоковой озвучки: %s", listener)


def unregister_stream_listener(listener: Callable[..., None]) -> None:
    """Удаляет ранее зарегистрированный обработчик потоковой озвучки."""

    with _STREAM_LOCK:
        try:
            _STREAM_LISTENERS.remove(listener)
        except ValueError:
            return
    log.info("Удалён слушатель потоковой озвучки: %s", listener)


def _notify_stream_listeners(
    pcm: bytes,
    sample_rate: int,
    *,
    text: str,
    preset: str,
    chunk_index: int,
    chunks_total: int,
    volume: float,
) -> None:
    """Передаёт синтезированный PCM всем подписчикам с подробными метаданными."""

    with _STREAM_LOCK:
        listeners = list(_STREAM_LISTENERS)
    for listener in listeners:
        try:
            listener(
                pcm,
                sample_rate,
                text=text,
                preset=preset,
                chunk_index=chunk_index,
                chunks_total=chunks_total,
                volume=volume,
            )
        except Exception:
            log.exception("Ошибка передачи PCM слушателю %s", listener)


def _perform_playback(audio_f32: np.ndarray, playback_rate: int, duration: float) -> None:
    """Проигрывает или имитирует проигрывание чанка речи."""

    if playback_rate <= 0:
        log.warning(
            "Пропускаю локальное воспроизведение: неверная частота",
            extra={"attrs": {"playback_rate": playback_rate}},
        )
        return

    end_time = time.perf_counter() + max(duration, 0.0)
    if _LOCAL_PLAYBACK_ENABLED:
        log.debug(
            "Воспроизводим чанк через локальные динамики",
            extra={"attrs": {"duration": round(max(duration, 0.0), 3)}},
        )
        sd.play(audio_f32, playback_rate, blocking=False)
        while time.perf_counter() < end_time:
            if _STOP_EVENT.is_set():
                break
            time.sleep(0.05)
        sd.stop()
    else:
        log.debug(
            "Локальное воспроизведение отключено, ожидаю завершения длительности",
            extra={"attrs": {"duration": round(max(duration, 0.0), 3)}},
        )
        while time.perf_counter() < end_time:
            if _STOP_EVENT.is_set():
                break
            time.sleep(0.05)

# ────────────────────────── 1. CONFIG ─────────────────────────────
# Идентификатор голоса Piper (файл <VOICE_ID>.onnx должен существовать)
VOICE_ID: str = "ru_RU-ruslan-medium"  # "ru-RU-irina-medium"  # default voice ID
# Пути, где ищем модель
SEARCH_DIRS: List[str] = ["./models/piper"]
# Максимальный размер чанка текста для озвучивания
MAX_CHARS: int = 180
# Пауза в секундах, добавляемая в конец каждого чанка, чтобы не обрезать фразу
TAIL_PAD_SEC: float = 0.3
# Единая целевая частота дискретизации для всех исходящих потоков звука.
# Значение 16 кГц совпадает с трактом робота и позволяет устранить треск,
# возникавший из-за пересчёта частоты на уровне прошивки.
TARGET_SAMPLE_RATE: int = 16_000
# Громкость по умолчанию.  Параметр оставлен в отдельной константе, чтобы
# при необходимости можно было аккуратно восстановить регулировку без
# пересборки всей логики озвучки.
DEFAULT_VOLUME: float = 1.0
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
VOICE_SAMPLE_RATE: int = VOICE.config.sample_rate
_SIG_HAS_AUDIO_STREAMING: bool = "audio_streaming" in inspect.signature(
    VOICE.synthesize
).parameters
log.info(
    "Модель голоса '%s' загружена (sr=%d, streaming=%s, target_sr=%d)",
    VOICE_ID,
    VOICE_SAMPLE_RATE,
    _SIG_HAS_AUDIO_STREAMING,
    TARGET_SAMPLE_RATE,
)

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

def _save_wav(path: str, pcm_i16: np.ndarray, sample_rate: int) -> None:
    """Пишет mono-PCM-16 bit в .wav (для отладки)."""
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)              # 16-bit
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_i16.tobytes())
    log.info(
        "WAV-debug: saved %s (%.2f s)",
        path,
        pcm_i16.size / sample_rate if sample_rate else 0.0,
    )


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
            attrs={"text": text, "emotion": "neutral"},
        )
    )
    _STOP_EVENT.clear()
    # Сначала приводим текст к виду, пригодному для TTS: удаляем мусорные
    # символы, числа переводим в слова и т.д.
    # Затем выполняем транслитерацию в кириллицу, чтобы Piper корректно
    # озвучивал даже латиницу.
    log.debug("Исходный текст для озвучки: %r", text)
    norm = translit(normalize_tts_text(text), "ru")
    log.debug("Нормализованный текст для TTS: %r", norm)
    log.info(
        "Озвучиваем строку длиной %d символов (target_sr=%d)",
        len(norm),
        TARGET_SAMPLE_RATE,
    )
    if emotion or preset != "neutral":
        log.warning(
            "Предустановленные эмоции временно отключены; используется базовый пресет",
            extra={"attrs": {"preset": preset, "emotion": emotion}},
        )
    if pitch not in (None, 1.0):
        log.warning(
            "Изменение высоты голоса отключено для соблюдения целевой частоты",
            extra={"attrs": {"pitch": pitch}},
        )
    if speed not in (None, 1.0):
        log.warning(
            "Изменение скорости речи отключено для стабилизации аудиотракта",
            extra={"attrs": {"speed": speed}},
        )

    pause = TAIL_PAD_SEC
    vol = DEFAULT_VOLUME

    now = time.time()
    # Периодическая очистка устаревшего кэша
    _cleanup_cache(now)

    playback_parts: list[np.ndarray] = []
    chunks = list(_split_by_sentences(norm, max_chars))
    total_chunks = len(chunks)

    for i, chunk in enumerate(chunks, 1):
        if _STOP_EVENT.is_set():
            break
        now = time.time()
        cache_file = _cache_path(chunk)
        pcm_i16_pad: np.ndarray | None = None
        chunk_source_rate = VOICE_SAMPLE_RATE
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
                    chunk_source_rate = wf.getframerate() or TARGET_SAMPLE_RATE
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
            tail = np.zeros(int(VOICE_SAMPLE_RATE * pause), np.int16)
            pcm_i16_pad = np.concatenate([pcm_i16, tail])
            chunk_source_rate = VOICE_SAMPLE_RATE

        # Применяем изменение высоты голоса согласно коэффициенту ``pitch``
        pcm_i16_pad = _apply_pitch(pcm_i16_pad, 1.0)

        # Приводим аудиоданные к целевой частоте 16 кГц.  Эта операция
        # выполняется даже для кэша, чтобы гарантировать единый формат.
        pcm_i16_pad = resample_pcm16(pcm_i16_pad, chunk_source_rate, TARGET_SAMPLE_RATE)

        need_cache_update = True
        try:
            stat_info = cache_file.stat()
            need_cache_update = (
                chunk_source_rate != TARGET_SAMPLE_RATE
                or now - stat_info.st_mtime > CACHE_TTL
            )
        except FileNotFoundError:
            need_cache_update = True

        if need_cache_update:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            _save_wav(str(cache_file), pcm_i16_pad, TARGET_SAMPLE_RATE)
            log.debug("Чанк %d сохранён в кэш %s", i, cache_file)

        playback_parts.append(pcm_i16_pad)
        audio_f32 = pcm_i16_pad.astype(np.float32) / 32767.0
        if vol != 1.0:
            audio_f32 = np.clip(audio_f32 * vol, -1.0, 1.0)

        playback_rate = TARGET_SAMPLE_RATE
        pcm_bytes = np.clip(audio_f32 * 32767.0, -32768, 32767).astype(np.int16)
        _notify_stream_listeners(
            pcm_bytes.tobytes(),
            playback_rate,
            text=chunk,
            preset="neutral",
            chunk_index=i,
            chunks_total=total_chunks,
            volume=vol,
        )

        # Запускаем воспроизведение. ``sd.wait`` занимает GIL,
        # и поэтому не даёт реагировать на слово «стоп».
        # Вместо этого ожидаем завершения в простом цикле с небольшим сном,
        # что даёт возможность другим потокам читать микрофон.
        duration = audio_f32.size / playback_rate if playback_rate else 0.0
        t1 = time.perf_counter()
        _perform_playback(audio_f32, playback_rate, duration)
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
        _save_wav(save_wav, full_audio, TARGET_SAMPLE_RATE)
    total_duration = full_audio.size / TARGET_SAMPLE_RATE if TARGET_SAMPLE_RATE else 0.0
    log.info("Озвучивание завершено: длительность %.2f с", total_duration)
    is_playing = False
    core_events.publish(core_events.Event(kind="speech.synthesis_finished"))
    _STOP_EVENT.clear()
    if _LOCAL_PLAYBACK_ENABLED:
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
    from utils.reply import extract_reply

    # Извлекаем текст ответа из возможного JSON.
    clean = extract_reply(text)
    if clean != text:
        log.debug("tts extracted reply: %r", clean)

    if get_request_source() == "telegram":
        try:
            import importlib
            from core.metrics import inc_metric

            tg = importlib.import_module("notifiers.telegram")
            tg.send(clean)
            log.info("telegram reply text=%r", clean)
            inc_metric("telegram.outgoing")
        except Exception as exc:  # pragma: no cover - защищаемся от сетевых ошибок
            log.warning("telegram reply failed: %s", exc)
        return

    import asyncio  # локальный импорт, чтобы не тянуть asyncio в синхронный контекст
    from functools import partial

    loop = loop or asyncio.get_running_loop()
    func = partial(working_tts, clean, preset=preset, pitch=pitch, speed=speed, emotion=emotion)
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
