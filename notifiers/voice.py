"""Фоновое озвучивание уведомлений с помощью Piper TTS.

Публичный API модуля — функция :func:`send`, которая добавляет текст в
очередь и при первом вызове запускает фонового воркера.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from typing import Any

from core.logging_json import TRACE_ID, configure_logging
from core.metrics import inc_metric, set_metric
from core.request_source import get_request_source
from memory.dialog_log import record_dialog_message
from utils.reply import extract_reply
from working_tts import speak_async

log = configure_logging("notifiers.voice")

# Очередь запросов на озвучивание.  Каждый элемент — словарь с полями
# ``text``, ``pitch``, ``speed`` и ``emotion``.
_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
# Задача-воркер, обрабатывающая очередь в фоне.
_worker_task: asyncio.Task | None = None
# Event loop, в котором живёт воркер. Может быть основным циклом приложения или
# отдельным потоком, если отправка TTS вызвана из чужого потока без loop.
_worker_loop: asyncio.AbstractEventLoop | None = None
_worker_thread: threading.Thread | None = None
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
                _queue.task_done()
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
    """Запустить фонового воркера, если он ещё не запущен.

    Если вызов происходит из стороннего потока без активного event loop, создаём
    собственный цикл и запускаем его в отдельном daemon‑потоке. Это устраняет
    предупреждения "coroutine was never awaited" и гарантирует доставку TTS.
    """

    global _worker_task, _worker_loop, _worker_thread

    running_loop: asyncio.AbstractEventLoop | None = None
    try:
        loop = asyncio.get_running_loop()
        _worker_loop = loop
        running_loop = loop
    except RuntimeError:
        if _worker_loop is None or _worker_loop.is_closed():
            # Создаём выделенный цикл и запускаем его в отдельном потоке.
            _worker_loop = asyncio.new_event_loop()
            running_loop = None

            def _run_loop() -> None:
                asyncio.set_event_loop(_worker_loop)
                log.debug("Запускаю выделенный event loop для TTS")
                _worker_loop.run_forever()

            _worker_thread = threading.Thread(
                target=_run_loop,
                daemon=True,
                name="voice-notifier-loop",
            )
            _worker_thread.start()
        loop = _worker_loop

    if loop is None:
        log.error("Не удалось создать event loop для TTS")
        return

    def _ensure_worker() -> None:
        nonlocal loop
        global _worker_task
        if _worker_task is None or _worker_task.done():
            _worker_task = loop.create_task(_worker())
            log.debug(
                "Фоновый воркер TTS запущен", extra={"attrs": {"loop_thread": threading.current_thread().name}}
            )

    if loop.is_running():
        if running_loop is loop:
            _ensure_worker()
        else:
            loop.call_soon_threadsafe(_ensure_worker)
    else:  # pragma: no cover - запускаем цикл в тестах при необходимости
        _ensure_worker()


def stop() -> None:
    """Аккуратно останавливает воркер и, при необходимости, вспомогательный loop."""

    global _worker_task, _worker_loop, _worker_thread

    try:
        running_loop = asyncio.get_running_loop()
    except RuntimeError:
        running_loop = None

    if _worker_task is not None:
        if _worker_loop is not None and _worker_loop.is_running():
            _worker_loop.call_soon_threadsafe(_worker_task.cancel)
            if running_loop is None or running_loop is not _worker_loop:
                async def _await_task(task: asyncio.Task) -> None:
                    with contextlib.suppress(asyncio.CancelledError):
                        await task

                future = asyncio.run_coroutine_threadsafe(
                    _await_task(_worker_task), _worker_loop
                )
                with contextlib.suppress(asyncio.CancelledError):
                    future.result(timeout=1.0)
        else:
            _worker_task.cancel()
        _worker_task = None

    if _worker_loop is not None and _worker_loop.is_running():
        _worker_loop.call_soon_threadsafe(_worker_loop.stop)
    if _worker_thread is not None and _worker_thread.is_alive():
        _worker_thread.join(timeout=1.0)

    _worker_loop = None
    _worker_thread = None


def say(text: str, *, pitch: float | None = None, speed: float | None = None, emotion: str | None = None) -> None:
    """Добавить *text* в очередь на озвучивание вместе с параметрами.

    ``pitch`` и ``speed`` задаются как коэффициенты, ``emotion`` — имя
    пресета из :data:`working_tts.TTS_PRESETS`.
    """
    # Перед постановкой в очередь пытаемся извлечь поле ``reply`` из JSON.
    clean = extract_reply(text)
    source = get_request_source()
    payload = {
        "text": clean,
        "pitch": pitch,
        "speed": speed,
        "emotion": emotion,
        "source": source,
    }

    loop = _worker_loop
    current_loop: asyncio.AbstractEventLoop | None
    try:
        current_loop = asyncio.get_running_loop()
    except RuntimeError:
        current_loop = None

    if loop is not None and loop.is_running():
        if current_loop is loop:
            _queue.put_nowait(payload)
        else:
            loop.call_soon_threadsafe(_queue.put_nowait, payload)
    else:
        _queue.put_nowait(payload)
    log.debug(
        "queued voice text=%r emotion=%s pitch=%s speed=%s source=%s",
        clean,
        emotion,
        pitch,
        speed,
        source,
    )
    set_metric("tts.queue_len", _queue.qsize())
    if source == "voice":
        try:
            record_dialog_message(
                clean,
                direction="outgoing",
                channel="voice",
                trace_id=TRACE_ID.get(),
                status="queued",
                metadata={"emotion": emotion, "pitch": pitch, "speed": speed},
            )
        except Exception:  # pragma: no cover - защита от сбоев БД
            log.exception("failed to log voice notification")


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
