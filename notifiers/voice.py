"""Фоновое озвучивание уведомлений с помощью Piper TTS.

Публичный API модуля — функция :func:`send`, которая добавляет текст в
очередь и при первом вызове запускает фонового воркера.
"""

from __future__ import annotations

import asyncio
from core.logging_json import configure_logging
from core.metrics import inc_metric, set_metric
from core.request_source import get_request_source
from working_tts import speak_async

log = configure_logging("notifiers.voice")

# Очередь запросов на озвучивание.  Каждый элемент — словарь с полями
# ``text``, ``pitch``, ``speed`` и ``emotion``.
_queue: asyncio.Queue[dict] = asyncio.Queue()
# Задача-воркер, обрабатывающая очередь в фоне.
_worker_task: asyncio.Task | None = None
# Публикуем метрики при старте: длина очереди и счётчик исходящих сообщений
# в Telegram.
set_metric("tts.queue_len", 0)
set_metric("telegram.outgoing", 0)


async def _worker() -> None:
    """Бесконечный цикл, озвучивающий запросы из очереди."""
    while True:
        # Получаем следующий элемент из очереди; структура описана выше.
        item = await _queue.get()
        try:
            set_metric("tts.queue_len", _queue.qsize())
            source = item.get("source", "voice")
            if source == "telegram":
                try:
                    import importlib

                    tg = importlib.import_module("notifiers.telegram")
                    tg.send(item["text"])
                    log.info("telegram reply text=%r", item["text"])
                    inc_metric("telegram.outgoing")
                except Exception as exc:  # pragma: no cover - сетевые ошибки не критичны
                    log.warning("telegram reply failed: %s", exc)
                continue

            await speak_async(
                item["text"],
                pitch=item.get("pitch"),
                speed=item.get("speed"),
                emotion=item.get("emotion"),
            )

            # Ранее здесь дублировались голосовые уведомления в Telegram.
            # По требованию пользователя отключаем такую логику: ответ
            # приходит только через тот канал, откуда поступил запрос.
            # Для удобства отладки фиксируем это событие в логах.
            log.debug(
                "skip telegram duplicate for voice source=%s text=%r",
                source,
                item["text"],
            )
        except Exception:  # pragma: no cover - логируем неожиданные ошибки
            log.exception("voice TTS failure")
        finally:
            _queue.task_done()
            set_metric("tts.queue_len", _queue.qsize())


def start() -> None:
    """Запустить фонового воркера, если он ещё не запущен."""
    global _worker_task
    if _worker_task is None:
        _worker_task = asyncio.create_task(_worker())


def say(text: str, *, pitch: float | None = None, speed: float | None = None, emotion: str | None = None) -> None:
    """Добавить *text* в очередь на озвучивание вместе с параметрами.

    ``pitch`` и ``speed`` задаются как коэффициенты, ``emotion`` — имя
    пресета из :data:`working_tts.TTS_PRESETS`.
    """
    source = get_request_source()
    _queue.put_nowait({
        "text": text,
        "pitch": pitch,
        "speed": speed,
        "emotion": emotion,
        "source": source,
    })
    log.debug(
        "queued voice text=%r emotion=%s pitch=%s speed=%s source=%s",
        text,
        emotion,
        pitch,
        speed,
        source,
    )
    set_metric("tts.queue_len", _queue.qsize())


def send(
    text: str,
    *,
    pitch: float | None = None,
    speed: float | None = None,
    emotion: str | None = None,
) -> None:
    """Публичная обёртка над :func:`say`.

    При первом вызове автоматически запускает воркер, чтобы TTS начал
    обрабатывать очередь сообщений.  Дополнительные параметры передаются
    в :func:`working_tts.speak_async`.
    """
    start()
    say(text, pitch=pitch, speed=speed, emotion=emotion)
