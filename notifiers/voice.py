"""Фоновое озвучивание уведомлений с помощью Piper TTS.

Публичный API модуля — функция :func:`send`, которая добавляет текст в
очередь и при первом вызове запускает фонового воркера.
"""

from __future__ import annotations

import asyncio
from core.logging_json import configure_logging
from core.metrics import inc_metric, set_metric
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
            await speak_async(
                item["text"],
                pitch=item.get("pitch"),
                speed=item.get("speed"),
                emotion=item.get("emotion"),
            )

            # Если активен Telegram-слушатель и он работает с владельцем,
            # дублируем текст голосового уведомления в личные сообщения.
            try:
                import importlib

                tg = importlib.import_module("notifiers.telegram")
                tl = importlib.import_module("notifiers.telegram_listener")

                if getattr(tl, "is_active", lambda: False)() and getattr(
                    tg._notifier, "_user_id", None
                ) == getattr(tl, "USER_ID", None):
                    tg.send(item["text"])
                    log.info("duplicate telegram text=%r", item["text"])
                    inc_metric("telegram.outgoing")
            except Exception as exc:  # pragma: no cover - защита от сетевых ошибок
                # Ошибки Telegram не должны мешать озвучиванию.
                log.warning("telegram duplicate failed: %s", exc)
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
    _queue.put_nowait({"text": text, "pitch": pitch, "speed": speed, "emotion": emotion})
    log.debug("queued voice text=%r emotion=%s pitch=%s speed=%s", text, emotion, pitch, speed)
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
