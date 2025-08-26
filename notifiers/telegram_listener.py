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
from typing import Any

import requests

from core.config import load_config
from core.logging_json import configure_logging
from core.metrics import inc_metric, set_metric
from app.command_processing import va_respond

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
# Разрешённый Telegram ID пользователя.
_USER_ID = _cfg.user.telegram_user_id


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


def listen(*, max_iterations: int | None = None) -> None:
    """Запуск бесконечного long polling цикла.

    Параметр ``max_iterations`` используется в тестах для ограничения
    количества запросов к API.  В рабочем режиме параметр не указывается,
    и функция будет работать бесконечно до остановки процесса.
    """

    offset = 0  # Указатель на последний обработанный update_id.
    iteration = 0

    while max_iterations is None or iteration < max_iterations:
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
                # Обновляем offset, чтобы следующий запрос начинался с
                # непрочитанных обновлений.
                offset = max(offset, update.get("update_id", 0) + 1)
                message = update.get("message") or {}
                chat_id = (message.get("chat") or {}).get("id")
                text = message.get("text")

                # Фильтруем по разрешённому пользователю и наличию текста.
                if chat_id != _USER_ID or not text:
                    log.debug(
                        "ignored update: chat_id=%s text=%r", chat_id, text
                    )
                    continue

                # Фиксируем метрику и передаём команду на обработку.
                inc_metric("telegram.incoming")
                log.info("incoming command: %r", text)
                try:
                    asyncio.run(va_respond(text))
                except Exception:  # pragma: no cover - на всякий случай логируем
                    log.exception("va_respond failed")
        except (requests.RequestException, ValueError) as exc:
            # Сетевые ошибки или некорректный JSON.  Логируем и пробуем
            # повторить запрос после небольшой паузы.
            log.warning("telegram poll failed: %s", exc)
            time.sleep(1)

