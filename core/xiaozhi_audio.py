"""Аудиомост между Telegram и облачным агентом Xiaozhi.

Модуль реализует полный цикл "текст → аудио → сервер Xiaozhi → аудио → текст",
чтобы чат в Telegram работал максимально близко к железным устройствам Xiaozhi,
которые общаются с сервером голосом. В коде много подробных логов на русском,
что упрощает диагностику сетевых и аудио-проблем.
"""

from __future__ import annotations

import io
import json
import logging
import wave
from contextlib import contextmanager
from typing import Callable, Tuple

from core.xiaozhi_client import XiaozhiClient

logger = logging.getLogger(__name__)


@contextmanager
def _temporary_stream_listener(listener: Callable, *, trace_id: str = ""):
    """Временная подписка на поток PCM из ``working_tts``.

    Контекст менеджер подключает слушателя, отключает локальное воспроизведение,
    а по выходу восстанавливает прежний флаг и удаляет слушателя, чтобы не
    оставить висящие колбэки. В логах фиксируется жизненный цикл для удобства
    отладки.
    """

    import working_tts

    logger.debug(
        "Подписываемся на поток PCM для Xiaozhi", extra={"trace_id": trace_id}
    )
    # Сохраняем старое значение флага, чтобы вернуть его после синтеза
    old_state = getattr(working_tts, "_LOCAL_PLAYBACK_ENABLED", True)
    working_tts.register_stream_listener(listener)
    working_tts.set_local_playback_enabled(False)
    try:
        yield
    finally:
        logger.debug(
            "Отписываемся от потока PCM для Xiaozhi", extra={"trace_id": trace_id}
        )
        working_tts.unregister_stream_listener(listener)
        working_tts.set_local_playback_enabled(old_state)


def synthesize_text_to_wav(text: str, *, trace_id: str = "") -> Tuple[bytes, int]:
    """Синтезирует текст в WAV-байты через ``working_tts``.

    Мы не воспроизводим звук локально, а собираем весь PCM из стриминговых
    колбэков и упаковываем его в моно WAV (16 kHz, 16-bit). Возвращается пара
    (байты_wav, sample_rate). Если синтез неожиданно не дал кадров, генерируем
    пустой WAV, чтобы вызывающий код мог зафиксировать ошибку в логах.
    """

    import working_tts

    pcm_chunks: list[bytes] = []
    sample_rate: int | None = None

    def _collector(pcm: bytes, sr: int, **kwargs) -> None:
        nonlocal sample_rate
        sample_rate = sr
        pcm_chunks.append(pcm)

    with _temporary_stream_listener(_collector, trace_id=trace_id):
        # Синтез выполняем синхронно, чтобы сразу после выхода из контекста
        # иметь готовые PCM-данные
        working_tts.working_tts(text, preset="neutral")

    merged_pcm = b"".join(pcm_chunks)
    if not sample_rate:
        sample_rate = 16000
        logger.warning(
            "Working TTS не вернул частоту дискретизации, используем 16 kHz по умолчанию",
            extra={"trace_id": trace_id},
        )

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(merged_pcm)

    wav_bytes = buffer.getvalue()
    logger.info(
        "Синтезировано аудио для Xiaozhi", extra={"trace_id": trace_id, "bytes": len(wav_bytes)}
    )
    return wav_bytes, sample_rate


def transcribe_wav_vosk(wav_bytes: bytes, *, trace_id: str = "") -> str:
    """Преобразует WAV-ответ от Xiaozhi в текст через Vosk.

    Модель загружается лениво из ``models/model_small``. На практике ответ
    Xiaozhi уже содержит речь ассистента, поэтому распознавание происходит
    быстро. Логи фиксируют этапы для удобства мониторинга.
    """

    import vosk

    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        sample_rate = wf.getframerate()
        pcm = wf.readframes(wf.getnframes())

    logger.debug(
        "Начинаем распознавание WAV от Xiaozhi", extra={"trace_id": trace_id, "rate": sample_rate}
    )
    model = vosk.Model("models/model_small")
    rec = vosk.KaldiRecognizer(model, sample_rate)
    rec.AcceptWaveform(pcm)
    result = json.loads(rec.Result())
    text = str(result.get("text", "")).strip()
    logger.info("Распознан ответ Xiaozhi: %s", text, extra={"trace_id": trace_id})
    return text


def chat_via_audio(client: XiaozhiClient, text: str, *, trace_id: str = "") -> str:
    """Полный цикл общения с Xiaozhi через аудио."""

    wav_bytes, sr = synthesize_text_to_wav(text, trace_id=trace_id)
    logger.debug(
        "Отправляем аудио-запрос в Xiaozhi", extra={"trace_id": trace_id, "rate": sr, "size": len(wav_bytes)}
    )
    response_wav = client.ask_audio(wav_bytes, trace_id=trace_id)
    logger.debug(
        "Получено аудио-ответ от Xiaozhi", extra={"trace_id": trace_id, "bytes": len(response_wav)}
    )
    return transcribe_wav_vosk(response_wav, trace_id=trace_id)

