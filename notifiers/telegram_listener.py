"""Приём команд от пользователя через Telegram-бота.

Модуль реализует цикл long polling метода ``getUpdates`` Telegram API.
Каждое полученное текстовое сообщение от владельца передаётся в
``app.command_processing.va_respond``.  Сообщения от других чатов
игнорируются.  Состояние ``offset`` учитывается, чтобы не обрабатывать
повторно уже прочитанные обновления.
"""

from __future__ import annotations

import asyncio
import time
import threading
from typing import Any

import requests

from core.config import load_config
from core.logging_json import configure_logging
from core.metrics import inc_metric, set_metric
from core.request_source import set_request_source, reset_request_source
from core import events as core_events

# Инициализируем логгер для удобной отладки модуля.
log = configure_logging("notifiers.telegram_listener")
# Публикуем метрику количества входящих сообщений.
set_metric("telegram.incoming", 0)

# Загружаем конфигурацию один раз при импорте.  Здесь содержатся токен
# Telegram-бота и ID пользователя, которому разрешено отправлять команды.
_cfg = load_config()
log.info(
    "config loaded: telegram_user_id=%s token_present=%s",
    _cfg.user.telegram_user_id,
    bool(_cfg.telegram.token),
)

# Формируем URL метода ``getUpdates`` с токеном бота.
_API_URL = f"https://api.telegram.org/bot{_cfg.telegram.token}/getUpdates"
# Разрешённый Telegram ID пользователя (владелец бота).
_USER_ID = _cfg.user.telegram_user_id
# Публичный алиас, чтобы другие модули могли проверить ID получателя.
USER_ID = _USER_ID
# Ссылка на обработчик команд; используется для подмены в тестах.
va_respond = None  # type: ignore[assignment]
# Флаг активности слушателя; используется для условной отправки дублирующих
# сообщений из голосового канала.
_RUNNING = False


def is_active() -> bool:
    """Возвращает ``True``, если слушатель сейчас запущен."""
    return _RUNNING


class _DummyResponse:
    """Примитивная обёртка ответа Telegram для тестов.

    Не используется в рабочем коде, но оставлена для возможного расширения
    и удобства unit-тестов.  При обычной работе модуль обращается напрямую
    к :mod:`requests`.
    """

    def __init__(self, data: dict[str, Any], status_code: int = 200) -> None:
        self._data = data
        self.status_code = status_code
        self.text = ""

    def json(self) -> dict[str, Any]:  # pragma: no cover - используется только в тестах
        return self._data


def listen(
    *,
    max_iterations: int | None = None,
    stop_event: threading.Event | None = None,
    loop: asyncio.AbstractEventLoop | None = None,
) -> None:
    """Запуск бесконечного long polling цикла.

    Параметр ``max_iterations`` используется в тестах для ограничения
    количества запросов к API.  Дополнительно можно передать ``stop_event``,
    чтобы корректно завершить цикл из другого потока.  Если передан
    ``loop``, обработчик команд будет выполняться в указанном цикле событий,
    что позволяет делегировать работу основному event loop ассистента и
    избегать проблем с временными циклами ``asyncio.run``.
    """

    offset = 0  # Указатель на последний обработанный update_id.
    iteration = 0

    while (
        (max_iterations is None or iteration < max_iterations)
        and not (stop_event and stop_event.is_set())
    ):
        iteration += 1
        try:
            # Выполняем запрос ``getUpdates`` с учётом текущего offset.
            resp = requests.get(
                _API_URL,
                params={"timeout": 30, "offset": offset},
                timeout=35,
            )
            data = resp.json()
            # Проверяем успешность ответа API.  Если код не 200 или
            # флаг ``ok`` равен False — просто пропускаем итерацию.
            if resp.status_code != 200 or not data.get("ok", False):
                log.warning(
                    "telegram api error: status=%s body=%s", resp.status_code, resp.text
                )
                continue

            for update in data.get("result", []):
                # ``update_id`` используется для расчёта ``offset``.
                update_id = update.get("update_id", 0)
                # Если сервер по какой‑то причине прислал уже обработанное
                # обновление — пропускаем его, чтобы не выполнять команду дважды.
                if update_id < offset:
                    log.debug("duplicate update_id: %s", update_id)
                    continue
                # Сдвигаем ``offset`` на следующий ID.
                offset = update_id + 1
                message = update.get("message") or {}
                chat_id = (message.get("chat") or {}).get("id")
                text = message.get("text")

                # Фильтруем по разрешённому пользователю и наличию текста.
                if chat_id != _USER_ID or not text:
                    log.debug(
                        "ignored update: chat_id=%s text=%r", chat_id, text
                    )
                    continue

                # Фиксируем метрику, публикуем событие и передаём команду на обработку.
                inc_metric("telegram.incoming")
                log.info("incoming command: %r", text)
                # Публикуем событие, чтобы другие подсистемы (например, проактивный
                # движок) могли реагировать на сообщения пользователя.
                core_events.publish(
                    core_events.Event(kind="telegram.message", attrs={"text": text})
                )
                try:
                    handler = va_respond
                    if handler is None:  # импортируем по требованию
                        from app.command_processing import va_respond as handler
                    token = set_request_source("telegram")
                    try:
                        # При отсутствии внешнего ``loop`` каждое сообщение
                        # обрабатывается отдельным временным циклом через
                        # ``asyncio.run``.  Однако такой подход мешает
                        # фоновой обработке уведомлений. Если же передан
                        # ``loop`` — используем его, чтобы задание выполнилось
                        # в основном event loop ассистента.
                        if loop is None:
                            asyncio.run(handler(text))
                        else:
                            fut = asyncio.run_coroutine_threadsafe(
                                handler(text), loop
                            )
                            fut.result()  # дожидаемся завершения
                            log.debug("handler executed in main loop")
                    finally:
                        reset_request_source(token)
                except Exception:  # pragma: no cover - на всякий случай логируем
                    log.exception("va_respond failed")
        except (requests.RequestException, ValueError) as exc:
            # Сетевые ошибки или некорректный JSON.  Логируем и пробуем
            # повторить запрос после небольшой паузы.
            log.warning("telegram poll failed: %s", exc)
            time.sleep(1)


async def launch(*, stop_event: threading.Event | None = None) -> None:
    """Асинхронный запуск слушателя в отдельном потоке."""

    global _RUNNING
    log.info("telegram listener started")
    _RUNNING = True
    try:
        # ``listen`` блокирует поток, поэтому выполняем его в пуле потоков.
        # Передаём текущий event loop, чтобы обработчик команд выполнялся
        # в нём и мог создавать фоновые задачи (TTS, метрики и т.д.).
        loop = asyncio.get_running_loop()
        await asyncio.to_thread(listen, stop_event=stop_event, loop=loop)
    except asyncio.CancelledError:
        # Отмена задачи при завершении приложения.
        log.info("telegram listener cancelled")
        raise
    except Exception:
        # Неожиданная ошибка — логируем для последующей диагностики.
        log.exception("telegram listener crashed")
        raise
    finally:
        # Отмечаем завершение работы слушателя.
        _RUNNING = False
        log.info("telegram listener stopped")

