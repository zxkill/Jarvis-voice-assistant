"""Фоновое озвучивание уведомлений с помощью Piper TTS.

Публичный API модуля — функция :func:`send`, которая добавляет текст в
очередь и при первом вызове запускает фонового воркера.
"""

from __future__ import annotations

import asyncio
from core.logging_json import configure_logging
from core.metrics import set_metric
from working_tts import speak_async

log = configure_logging("notifiers.voice")

# Очередь текстов на озвучивание.
_queue: asyncio.Queue[str] = asyncio.Queue()
# Задача-воркер, обрабатывающая очередь в фоне.
_worker_task: asyncio.Task | None = None
# Публикуем метрику длины очереди сразу при старте.
set_metric("tts.queue_len", 0)


async def _worker() -> None:
    """Бесконечный цикл, извлекающий тексты из очереди и озвучивающий их."""
    while True:
        # Получаем следующий текст из очереди (ожидаем, если её нет)
        text = await _queue.get()
        try:
            # Обновляем метрику длины очереди и запускаем TTS
            set_metric("tts.queue_len", _queue.qsize())
            await speak_async(text)
        except Exception:  # pragma: no cover - логируем неожиданные ошибки
            log.exception("voice TTS failure")
        finally:
            # Сообщаем очереди об обработке элемента и снова обновляем метрику
            _queue.task_done()
            set_metric("tts.queue_len", _queue.qsize())


def start() -> None:
    """Запустить фонового воркера, если он ещё не запущен."""
    global _worker_task
    if _worker_task is None:
        _worker_task = asyncio.create_task(_worker())


def say(text: str) -> None:
    """Добавить *text* в очередь на озвучивание."""
    _queue.put_nowait(text)
    set_metric("tts.queue_len", _queue.qsize())


def send(text: str) -> None:
    """Публичная обёртка над :func:`say`.

    При первом вызове автоматически запускает воркер, чтобы TTS начал
    обрабатывать очередь сообщений.
    """
    start()
    say(text)
