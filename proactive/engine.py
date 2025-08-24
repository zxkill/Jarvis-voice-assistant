from __future__ import annotations

"""Движок проактивных подсказок.

Подписывается на новые сообщения из брокера событий, запрашивает у
``Policy`` подходящий канал и отправляет уведомление через выбранный
нотификатор.
"""

from core.logging_json import configure_logging
from core.metrics import inc_metric, set_metric
from proactive.policy import Policy
from memory.db import get_connection
from core import events as core_events


class ProactiveEngine:
    """Обрабатывает проактивные подсказки и отправляет их пользователю."""

    def __init__(self, policy: Policy) -> None:
        # ``policy`` определяет канал доставки подсказок.
        self.policy = policy
        # Считаем, что пользователь присутствует, пока не получили событие.
        self.present = True
        self.log = configure_logging("proactive.engine")
        set_metric("suggestions.sent", 0)
        set_metric("suggestions.failed", 0)
        # Подписываемся на новые подсказки и обновления присутствия.
        core_events.subscribe("suggestion.created", self._on_suggestion)
        core_events.subscribe("presence.update", self._on_presence)

    # ------------------------------------------------------------------
    def _on_presence(self, event: core_events.Event) -> None:
        """Обновить текущее состояние присутствия."""
        # Событие содержит атрибут ``present`` со значением ``True/False``.
        self.present = bool(event.attrs.get("present"))

    # ------------------------------------------------------------------
    def _on_suggestion(self, event: core_events.Event) -> None:
        """Получить новую подсказку и отправить её согласно политике."""
        text = event.attrs.get("text", "")
        reason_code = event.attrs.get("reason_code", "")
        suggestion_id = int(event.attrs.get("suggestion_id", 0))
        # Запрашиваем у политики канал доставки, учитывающий присутствие
        # и ограничения (тихое время, троттлинг и т.п.).
        channel = self.policy.choose_channel(self.present)
        # Логируем принятое политикой решение.
        self.log.info(
            "policy result",
            extra={"ctx": {"suggestion_id": suggestion_id, "channel": channel}},
        )
        if channel is None:
            # Троттлинг запретил отправку — помечаем подсказку и выходим.
            self._mark_processed(suggestion_id)
            return

        # Если выбран голосовой канал, дополнительно отправляем сообщение
        # в Telegram как резервный вариант. Подсказка считается
        # обработанной, если хотя бы один канал отправил её успешно.
        channels = [channel]
        if channel == "voice":
            channels.append("telegram")

        sent = False
        for ch in channels:
            sent = self._send(ch, text) or sent
        if sent:
            self._mark_processed(suggestion_id)

    # ------------------------------------------------------------------
    def _send(self, channel: str, text: str) -> bool:
        """Отправить текст через указанный канал."""
        try:
            if channel == "voice":
                # Отправка через голосовой синтезатор.
                from notifiers import voice as notifier
            else:
                # Текстовое сообщение в Telegram-бот.
                from notifiers import telegram as notifier
            notifier.send(text)
            self.log.info(
                "sent",
                extra={"ctx": {"channel": channel, "text": text}},
            )
            inc_metric("suggestions.sent")
            return True
        except Exception:
            # Любая ошибка уведомления логируется, но не выбрасывается.
            self.log.exception(
                "send failed",
                extra={"ctx": {"channel": channel}},
            )
            inc_metric("suggestions.failed")
            return False

    # ------------------------------------------------------------------
    def _mark_processed(self, suggestion_id: int) -> None:
        """Пометить подсказку как обработанную."""
        # В таблице ``suggestions`` выставляется флаг ``processed=1``.
        with get_connection() as conn:
            conn.execute(
                "UPDATE suggestions SET processed = 1 WHERE id = ?",
                (suggestion_id,),
            )
