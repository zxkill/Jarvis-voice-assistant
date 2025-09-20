"""Отправка личных сообщений владельцу через Telegram.

Публичный API модуля — функции :func:`send` и :func:`send_action`,
использующие предсозданный экземпляр :class:`TelegramNotifier`.
"""

from __future__ import annotations

import requests

from core.logging_json import configure_logging, TRACE_ID
from core.metrics import inc_metric, set_metric
from core.config import load_config
from utils.reply import extract_reply
from memory.dialogs import log_message

log = configure_logging("notifiers.telegram")
# Счётчик неудачных попыток отправки сообщений.
set_metric("telegram.failures", 0)

# Загружаем конфигурацию при импорте модуля.
_cfg = load_config()
log.info(
    "config loaded: telegram_user_id=%s token_present=%s",
    _cfg.user.telegram_user_id,
    bool(_cfg.telegram.token),
)


class TelegramNotifier:
    """Класс, отправляющий владельцу прямые сообщения."""

    def __init__(self, token: str, user_id: int) -> None:
        # Готовим URL метода ``sendMessage`` с токеном бота.
        self._api = f"https://api.telegram.org/bot{token}/sendMessage"
        # Отдельный URL для ``sendChatAction`` показывает в чате, что бот печатает.
        self._api_action = f"https://api.telegram.org/bot{token}/sendChatAction"
        # Telegram ID пользователя, которому адресуются уведомления.
        self._user_id = user_id

    def send(self, text: str) -> None:
        """Отправить сообщение *text* владельцу.

        Если *text* представляет собой JSON с полем ``reply``,
        в Telegram отправляется только значение этого поля.
        """
        clean = extract_reply(text)
        log.debug("telegram send text=%r", clean)
        try:
            resp = requests.post(
                self._api,
                json={"chat_id": self._user_id, "text": clean},
                timeout=5,
            )
            data = resp.json()
            # Проверяем успешность ответа API.
            if resp.status_code != 200 or not data.get("ok", False):
                log.warning("telegram api error: %s %s", resp.status_code, resp.text)
                inc_metric("telegram.failures")
            else:
                log_message(
                    clean,
                    direction="outgoing",
                    channel="telegram",
                    trace_id=TRACE_ID.get(),
                )
        except (requests.RequestException, ValueError) as exc:
            # Сеть недоступна или получен некорректный JSON.
            log.warning("telegram request failed: %s", exc)
            inc_metric("telegram.failures")

    def send_action(self, action: str = "typing") -> None:
        """Отправить пользователю индикатор действия.

        Используется для отображения статуса «печатает…», чтобы собеседник
        видел, что ассистент формирует ответ. По умолчанию применяется
        действие ``typing``.
        """
        log.debug("telegram send action=%s", action)
        try:
            requests.post(
                self._api_action,
                json={"chat_id": self._user_id, "action": action},
                timeout=5,
            )
        except requests.RequestException as exc:  # pragma: no cover - сетевые сбои
            log.warning("telegram action failed: %s", exc)


# Создаём уведомитель после определения класса.
_notifier = TelegramNotifier(
    token=_cfg.telegram.token,
    user_id=_cfg.user.telegram_user_id,
)


def send(text: str) -> None:
    """Публичная обёртка, отправляющая сообщение владельцу."""
    _notifier.send(text)


def send_action(action: str = "typing") -> None:
    """Публичная обёртка для отправки индикатора действия."""
    _notifier.send_action(action)
