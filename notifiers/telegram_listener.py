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
from datetime import datetime
from typing import Any

import requests

from core.config import load_config
from core.logging_json import configure_logging, TRACE_ID, new_trace_id
from core.metrics import inc_metric, set_metric
from core.request_source import set_request_source, reset_request_source
from core import events as core_events
from memory.dialogs import fetch_history

# ────────────────────────── ИНИЦИАЛИЗАЦИЯ ──────────────────────────

# Инициализируем логгер для удобной отладки модуля.
log = configure_logging("notifiers.telegram_listener")
# Публикуем метрику количества входящих сообщений.
set_metric("telegram.incoming", 0)


def _validate_config(cfg: Any) -> None:
    """Проверяем обязательные поля конфигурации Telegram."""

    missing: list[str] = []
    if not getattr(cfg.telegram, "token", ""):
        missing.append("telegram.token")
    if not getattr(cfg.user, "telegram_user_id", 0):
        missing.append("user.telegram_user_id")
    if missing:
        message = ", ".join(missing)
        log.error(
            "telegram listener config is incomplete: %s",
            message,
        )
        raise RuntimeError(f"telegram config missing: {message}")


# Загружаем конфигурацию один раз при импорте.  Здесь содержатся токен
# Telegram-бота и ID пользователя, которому разрешено отправлять команды.
_cfg = load_config()
_validate_config(_cfg)
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
# Отметка времени старта модуля — используется в команде ``/status``.
_STARTED_AT = time.time()


def _send_telegram_message(text: str) -> None:
    """Отправка служебного сообщения владельцу через Telegram."""

    try:
        from notifiers import telegram as notifier
    except Exception:  # pragma: no cover - крайне редкий случай сбоя импорта
        log.exception("failed to import telegram notifier for control reply")
        return

    log.debug("control reply: %s", text)
    try:
        notifier.send(text)
    except Exception:  # pragma: no cover - сетевые ошибки отлавливает notifier
        log.exception("failed to send control reply via telegram")


def _format_uptime(seconds: float) -> str:
    """Преобразуем продолжительность в человекочитаемый формат."""

    total_seconds = int(max(seconds, 0))
    minutes, sec = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    parts: list[str] = []
    if days:
        parts.append(f"{days} д")
    if hours:
        parts.append(f"{hours} ч")
    if minutes:
        parts.append(f"{minutes} мин")
    if not parts:
        parts.append(f"{sec} с")
    return " ".join(parts)


def _render_history(limit: int) -> str:
    """Формируем текст с последними сообщениями диалога."""

    history = fetch_history(limit=limit, channel="telegram", ascending=False)
    if not history:
        return "История пуста — пока не о чем рассказывать."

    lines: list[str] = ["Последние сообщения:"]
    # Записи уже отсортированы по убыванию времени; разворачиваем, чтобы
    # выводить в естественном порядке от старых к новым.
    for item in reversed(history):
        direction = "→" if item["direction"] == "outgoing" else "←"
        timestamp = datetime.fromtimestamp(item["ts"]).strftime("%H:%M:%S")
        text = item["text"].strip() or "(пусто)"
        lines.append(f"{timestamp} {direction} {text}")
    return "\n".join(lines)


def _handle_control_command(text: str) -> bool:
    """Обрабатываем служебные команды Telegram и возвращаем ``True`` если выполнено."""

    clean = text.strip()
    if not clean.startswith("/"):
        return False

    command, *args = clean.split()
    name = command.lower()
    log.debug("control command received: %s args=%s", name, args)

    if name in {"/start", "/help"}:
        help_text = (
            "Привет! Доступные команды:\n"
            "• /help — подсказка по возможностям\n"
            "• /status — состояние ассистента\n"
            "• /history [n] — последние n сообщений (по умолчанию 10)\n"
            "Просто напиши сообщение, и я выполню команду или отвечу."
        )
        _send_telegram_message(help_text)
        return True

    if name == "/status":
        uptime = _format_uptime(time.time() - _STARTED_AT)
        status = (
            "Ассистент активен.\n"
            f"Идентификатор владельца: {_USER_ID}\n"
            f"Uptime: {uptime}\n"
            f"Очередь long polling: {'активна' if _RUNNING else 'остановлена'}"
        )
        _send_telegram_message(status)
        return True

    if name == "/history":
        default_limit = 10
        limit = default_limit
        if args:
            try:
                limit = max(1, min(50, int(args[0])))
            except ValueError:
                _send_telegram_message("Нужно указать число от 1 до 50.")
                return True
        history_text = _render_history(limit)
        _send_telegram_message(history_text)
        return True

    log.debug("unknown control command: %s", name)
    return False


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

                # Сначала проверяем, не является ли сообщение служебной командой.
                if _handle_control_command(text):
                    log.debug("control command handled", extra={"ctx": {"text": text}})
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
                    trace_token = TRACE_ID.set(new_trace_id())
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
                        TRACE_ID.reset(trace_token)
                except Exception:  # pragma: no cover - на всякий случай логируем
                    # Добавляем текст команды в ``attrs``, чтобы понимать, что именно
                    # привело к исключению внутри обработчика.
                    log.exception(
                        "va_respond failed", extra={"attrs": {"text": text}}
                    )
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

