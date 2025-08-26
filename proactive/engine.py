from __future__ import annotations

"""Движок проактивных подсказок.

Подписывается на новые сообщения из брокера событий, запрашивает у
``Policy`` подходящий канал и отправляет уведомление через выбранный
нотификатор.
"""

import threading
import time
import datetime as dt

from core.logging_json import configure_logging
from core.metrics import inc_metric, set_metric
from proactive.policy import Policy
from memory.db import get_connection, get_last_smalltalk_ts, set_last_smalltalk_ts
from core import events as core_events
from skills.chit_chat_ru import random_phrase


class ProactiveEngine:
    """Обрабатывает проактивные подсказки и отправляет их пользователю."""

    def __init__(
        self,
        policy: Policy,
        *,
        idle_threshold_sec: int = 300,
        smalltalk_interval_sec: int = 3600,
        check_period_sec: int = 30,
    ) -> None:
        # ``policy`` определяет канал доставки подсказок.
        self.policy = policy
        # Считаем, что пользователь присутствует, пока не получили событие.
        self.present = True
        # Последняя голосовая команда, чтобы отслеживать длительное молчание.
        self._last_command_ts = time.time()
        # Параметры троттлинга small-talk.
        self.idle_threshold_sec = idle_threshold_sec
        self.smalltalk_interval_sec = smalltalk_interval_sec
        self.check_period_sec = check_period_sec
        self.log = configure_logging("proactive.engine")
        set_metric("suggestions.sent", 0)
        set_metric("suggestions.failed", 0)
        # Подписываемся на новые подсказки, обновления присутствия и команды.
        core_events.subscribe("suggestion.created", self._on_suggestion)
        core_events.subscribe("presence.update", self._on_presence)
        core_events.subscribe("speech.recognized", self._on_command)
        # Фоновая проверка тишины запускается в отдельном потоке.
        threading.Thread(target=self._idle_loop, daemon=True).start()

    # ------------------------------------------------------------------
    def _on_presence(self, event: core_events.Event) -> None:
        """Обновить текущее состояние присутствия."""
        # Событие содержит атрибут ``present`` со значением ``True/False``.
        self.present = bool(event.attrs.get("present"))

    # ------------------------------------------------------------------
    def _on_command(self, event: core_events.Event) -> None:
        """Запоминаем время последней распознанной команды."""
        self._last_command_ts = time.time()

    # ------------------------------------------------------------------
    def _on_suggestion(self, event: core_events.Event) -> None:
        """Получить новую подсказку и отправить её согласно политике."""
        text = event.attrs.get("text", "")
        reason_code = event.attrs.get("reason_code", "")
        suggestion_id = int(event.attrs.get("suggestion_id", 0))
        period = event.attrs.get("period")
        weekday = event.attrs.get("weekday")
        # Логируем сам факт получения подсказки и её причину.
        self.log.info(
            "suggestion received",
            extra={
                "ctx": {
                    "suggestion_id": suggestion_id,
                    "reason_code": reason_code,
                    "period": period,
                    "weekday": weekday,
                }
            },
        )
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
            sent = (
                self._send(ch, text, reason_code=reason_code, period=period, weekday=weekday)
                or sent
            )
        if sent:
            self._mark_processed(suggestion_id)

    # ------------------------------------------------------------------
    def _send(
        self,
        channel: str,
        text: str,
        *,
        reason_code: str,
        period: str | None,
        weekday: str | None,
    ) -> bool:
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
                extra={
                    "ctx": {
                        "channel": channel,
                        "text": text,
                        "reason_code": reason_code,
                        "period": period,
                        "weekday": weekday,
                    }
                },
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
    def _idle_loop(self) -> None:
        """Фоновый поток, генерирующий small-talk при длительном молчании."""
        while True:
            time.sleep(self.check_period_sec)
            if not self.present:
                continue
            now = time.time()
            if now - self._last_command_ts < self.idle_threshold_sec:
                continue
            last_smalltalk = get_last_smalltalk_ts()
            if now - last_smalltalk < self.smalltalk_interval_sec:
                continue
            text = random_phrase()
            now_dt = dt.datetime.fromtimestamp(now)
            period = self._period_of_day(now_dt.hour)
            weekday = now_dt.strftime("%A")
            self.log.info(
                "smalltalk triggered",
                extra={
                    "ctx": {
                        "reason": "long_silence",
                        "period": period,
                        "weekday": weekday,
                    }
                },
            )
            core_events.publish(
                core_events.Event(
                    kind="suggestion.created",
                    attrs={
                        "text": text,
                        "reason_code": "long_silence",
                        "period": period,
                        "weekday": weekday,
                    },
                )
            )
            set_last_smalltalk_ts(int(now))

    # ------------------------------------------------------------------
    @staticmethod
    def _period_of_day(hour: int) -> str:
        """Вернуть период суток по номеру часа."""
        if 5 <= hour < 12:
            return "morning"
        if 12 <= hour < 17:
            return "day"
        if 17 <= hour < 23:
            return "evening"
        return "night"

    # ------------------------------------------------------------------
    def _mark_processed(self, suggestion_id: int) -> None:
        """Пометить подсказку как обработанную."""
        # В таблице ``suggestions`` выставляется флаг ``processed=1``.
        with get_connection() as conn:
            conn.execute(
                "UPDATE suggestions SET processed = 1 WHERE id = ?",
                (suggestion_id,),
            )
