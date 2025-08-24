"""Отправка личных сообщений владельцу через Telegram.

Публичный API модуля — функция :func:`send`, использующая
предсозданный экземпляр :class:`TelegramNotifier`.
"""

from __future__ import annotations

import requests

from core.logging_json import configure_logging
from core.metrics import inc_metric, set_metric
from core.config import load_config

log = configure_logging("notifiers.telegram")
# Счётчик неудачных попыток отправки сообщений.
set_metric("telegram.failures", 0)

# Загружаем конфигурацию при импорте модуля.
_cfg = load_config()


class TelegramNotifier:
    """Класс, отправляющий владельцу прямые сообщения."""

    def __init__(self, token: str, user_id: int) -> None:
        # Готовим URL метода ``sendMessage`` с токеном бота.
        self._api = f"https://api.telegram.org/bot{token}/sendMessage"
        # Telegram ID пользователя, которому адресуются уведомления.
        self._user_id = user_id

    def send(self, text: str) -> None:
        """Отправить сообщение *text* владельцу."""
        try:
            resp = requests.post(
                self._api,
                json={"chat_id": self._user_id, "text": text},
                timeout=5,
            )
            data = resp.json()
            # Проверяем успешность ответа API.
            if resp.status_code != 200 or not data.get("ok", False):
                log.warning("telegram api error: %s %s", resp.status_code, resp.text)
                inc_metric("telegram.failures")
        except (requests.RequestException, ValueError) as exc:
            # Сеть недоступна или получен некорректный JSON.
            log.warning("telegram request failed: %s", exc)
            inc_metric("telegram.failures")


# Создаём уведомитель после определения класса.
_notifier = TelegramNotifier(
    token=_cfg.telegram.token,
    user_id=_cfg.user.telegram_user_id,
)


def send(text: str) -> None:
    """Публичная обёртка, отправляющая сообщение владельцу."""
    _notifier.send(text)
