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
from memory.writer import add_suggestion_feedback
from core import events as core_events
from skills.chit_chat_ru import random_phrase

# Глобальная ссылка на последний созданный экземпляр движка.
_engine_instance: "ProactiveEngine | None" = None


def is_awaiting_response() -> bool:
    """Проверить, ждём ли мы сейчас ответа на подсказку."""

    awaiting = bool(_engine_instance and _engine_instance._awaiting)
    if _engine_instance:
        _engine_instance.log.debug("awaiting response: %s", awaiting)
    return awaiting


def pop_awaiting() -> dict | None:
    """Получить информацию об ожидаемой подсказке и сбросить флаг ожидания."""

    if not _engine_instance or not _engine_instance._awaiting:
        if _engine_instance:
            _engine_instance.log.debug("no suggestion awaiting")
        return None
    timer = _engine_instance._awaiting.get("timer")
    if timer:
        timer.cancel()
    info = _engine_instance._awaiting
    _engine_instance._awaiting = None
    _engine_instance.log.debug(
        "awaiting consumed", extra={"ctx": {"suggestion_id": info.get("id")}}
    )
    return info


class ProactiveEngine:
    """Обрабатывает проактивные подсказки и отправляет их пользователю."""

    def __init__(
        self,
        policy: Policy,
        *,
        idle_threshold_sec: int = 300,
        smalltalk_interval_sec: int = 3600,
        check_period_sec: int = 30,
        response_timeout_sec: int = 10,
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
        # Таймаут ожидания ответа пользователя на подсказку.
        self.response_timeout_sec = response_timeout_sec
        # Состояние ожидания ответа: хранит ID подсказки и таймер.
        self._awaiting: dict | None = None
        self.log = configure_logging("proactive.engine")
        # Сохраняем глобальную ссылку на экземпляр движка,
        # чтобы другие модули могли проверить состояние ожидания.
        global _engine_instance
        _engine_instance = self
        # Регистрируем метрики отправки и откликов пользователя
        set_metric("suggestions.sent", 0)
        set_metric("suggestions.failed", 0)
        set_metric("suggestions.responded", 0)
        set_metric("suggestions.accepted", 0)
        set_metric("suggestions.declined", 0)
        # Подписываемся на новые подсказки, обновления присутствия и команды.
        core_events.subscribe("suggestion.created", self._on_suggestion)
        core_events.subscribe("presence.update", self._on_presence)
        # Подписка на распознанную речь используется и для обновления таймера,
        # и для фиксации возможного ответа на подсказку.
        core_events.subscribe("speech.recognized", self._on_command)
        # Ответы, пришедшие текстом, обрабатываем напрямую, а голосовые
        # перехватываются в ``app.command_processing``.
        core_events.subscribe("telegram.message", self._on_user_response)
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
        trace_id = event.attrs.get("trace_id")  # сквозной идентификатор цепочки
        # Берём флаг присутствия из события, чтобы корректно
        # обработать подсказки, сгенерированные в режиме ``absent``.
        present = bool(event.attrs.get("present", self.present))
        # Логируем сам факт получения подсказки и её причину.
        self.log.info(
            "suggestion received",
            extra={
                "ctx": {
                    "suggestion_id": suggestion_id,
                    "reason_code": reason_code,
                    "period": period,
                    "weekday": weekday,
                    "present": present,
                    "trace_id": trace_id,
                }
            },
        )
        # Запрашиваем у политики канал доставки, учитывающий присутствие
        # и ограничения (тихое время, троттлинг и т.п.).
        channel = self.policy.choose_channel(present)
        # Логируем принятое политикой решение.
        self.log.info(
            "policy result",
            extra={
                "ctx": {
                    "suggestion_id": suggestion_id,
                    "channel": channel,
                    "present": present,
                    "trace_id": trace_id,
                }
            },
        )
        if channel is None:
            # Троттлинг запретил отправку — помечаем подсказку и выходим.
            self._mark_processed(suggestion_id)
            return

        # Подсказку отправляем только по одному каналу, выбранному политикой.
        # Ранее голосовые уведомления дублировались в Telegram «на всякий случай»,
        # но практика показала, что это приводит к лишнему шуму и путанице.
        # Поэтому теперь отказались от дублирования, оставляя лишь основной канал.
        channels = [channel]

        sent = False
        for ch in channels:
            sent = (
                self._send(
                    ch,
                    text,
                    reason_code=reason_code,
                    period=period,
                    weekday=weekday,
                    trace_id=trace_id,
                )
                or sent
            )
        if sent:
            self._mark_processed(suggestion_id)
            # Если у подсказки есть ID — ожидаем ответ пользователя.
            if suggestion_id:
                self._await_response(suggestion_id, text, trace_id)

    # ------------------------------------------------------------------
    def _send(
        self,
        channel: str,
        text: str,
        *,
        reason_code: str,
        period: str | None,
        weekday: str | None,
        trace_id: str | None,
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
                        "trace_id": trace_id,
                    }
                },
            )
            inc_metric("suggestions.sent")
            return True
        except Exception:
            # Любая ошибка уведомления логируется, но не выбрасывается.
            self.log.exception(
                "send failed",
                extra={"ctx": {"channel": channel, "trace_id": trace_id}},
            )
            inc_metric("suggestions.failed")
            return False

    # ------------------------------------------------------------------
    def _idle_loop(self) -> None:
        """Фоновый поток, генерирующий small-talk при длительном молчании."""
        while True:
            time.sleep(self.check_period_sec)
            # Small-talk должен работать даже когда пользователя нет рядом:
            # политика доставки самостоятельно выберет Telegram.
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
                        "present": self.present,
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
                        "present": self.present,
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

    # ------------------------------------------------------------------
    def _await_response(self, suggestion_id: int, text: str, trace_id: str | None) -> None:
        """Перейти в режим ожидания ответа пользователя.

        Запускается таймер и публикуется событие изменения контекста,
        чтобы другие компоненты могли знать, что система ждёт реакцию.
        """

        # Отменяем предыдущий таймер, если он ещё активен, чтобы не получить
        # несколько одновременных ожиданий.
        if self._awaiting and (timer := self._awaiting.get("timer")):
            timer.cancel()

        timer = threading.Timer(self.response_timeout_sec, self._response_timeout)
        # Сохраняем информацию о ожидаемом ответе вместе с ``trace_id``
        self._awaiting = {
            "id": suggestion_id,
            "text": text,
            "timer": timer,
            "trace_id": trace_id,
        }
        timer.start()
        # Публикуем событие о переходе в режим ожидания ответа.
        core_events.publish(
            core_events.Event(
                kind="context.set",
                attrs={
                    "state": "awaiting_suggestion_response",
                    "suggestion_id": suggestion_id,
                    "trace_id": trace_id,
                },
            )
        )
        self.log.debug(
            "awaiting response",
            extra={
                "ctx": {
                    "suggestion_id": suggestion_id,
                    "timeout_sec": self.response_timeout_sec,
                    "trace_id": trace_id,
                }
            },
        )

    # ------------------------------------------------------------------
    def _response_timeout(self) -> None:
        """Обработать истечение времени ожидания ответа."""
        if not self._awaiting:
            return
        suggestion_id = self._awaiting.get("id")
        trace_id = self._awaiting.get("trace_id")
        self._awaiting = None
        self.log.info(
            "response timeout",
            extra={"ctx": {"suggestion_id": suggestion_id, "trace_id": trace_id}},
        )

    # ------------------------------------------------------------------
    def _on_user_response(self, event: core_events.Event) -> None:
        """Проверить, является ли текст ответом на ожидаемую подсказку."""
        if not self._awaiting:
            return
        text = (event.attrs.get("text") or "").strip()
        if not text:
            return
        suggestion_id = self._awaiting["id"]
        trace_id = self._awaiting.get("trace_id")
        timer = self._awaiting.get("timer")
        if timer:
            timer.cancel()
        self._awaiting = None
        accepted = self._is_positive(text)
        # Сохраняем отзыв и публикуем событие для остальных компонентов.
        add_suggestion_feedback(suggestion_id, text, accepted)
        core_events.publish(
            core_events.Event(
                kind="suggestion.response",
                attrs={
                    "suggestion_id": suggestion_id,
                    "text": text,
                    "accepted": accepted,
                    "trace_id": trace_id,
                },
            )
        )
        self.log.info(
            "response received",
            extra={
                "ctx": {
                    "suggestion_id": suggestion_id,
                    "accepted": accepted,
                    "trace_id": trace_id,
                }
            },
        )
        # Обновляем метрики откликов
        inc_metric("suggestions.responded")
        if accepted:
            inc_metric("suggestions.accepted")
        else:
            inc_metric("suggestions.declined")
        # После фиксации ответа отправляем пользователю небольшое подтверждение
        # в Telegram, чтобы он видел, что система приняла реплику.  Это помогает
        # при удалённом управлении и упрощает отладку.
        try:
            from notifiers import telegram as notifier

            reply = (
                "Отлично, записал" if accepted else "Хорошо, отложим"
            )
            notifier.send(reply)
            self.log.debug(
                "ack sent",
                extra={
                    "ctx": {
                        "suggestion_id": suggestion_id,
                        "accepted": accepted,
                        "trace_id": trace_id,
                    }
                },
            )
        except Exception:
            # Логируем, но не прерываем обработку при ошибке отправки подтверждения.
            self.log.exception(
                "ack failed",
                extra={"ctx": {"suggestion_id": suggestion_id, "trace_id": trace_id}},
            )

    # ------------------------------------------------------------------
    @staticmethod
    def _is_positive(text: str) -> bool:
        """Определить, является ли ответ пользователя положительным.

        Простейшая эвристика по ключевым словам: ``да``, ``ок``,
        ``хорошо`` трактуются как согласие. Всё остальное считается
        отказом.  Логику можно расширять при необходимости.
        """

        text = text.lower()
        positives = ("да", "ок", "хорошо", "ладно")
        negatives = ("нет", "не", "потом", "позже")
        if any(word in text for word in positives):
            return True
        if any(word in text for word in negatives):
            return False
        # По умолчанию считаем ответ отрицательным, чтобы не завышать статистику.
        return False
