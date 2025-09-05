from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field as dataclass_field

from core.logging_json import configure_logging
from core.metrics import inc_metric, set_metric
from core.quiet import is_quiet_now


@dataclass
class PolicyConfig:
    """Параметры выбора канала для проактивных подсказок.

    Attributes
    ----------
    force_telegram:
        Если ``True``, все подсказки отправляются только в Telegram,
        игнорируя прочие правила.
    silence_window:
        Период времени (``start`` → ``end``), в течение которого
        запрещено проигрывать голосовые уведомления.
    suggestion_min_interval_min:
        Минимальный интервал в минутах между подсказками. Используется
        для троттлинга отправок.
    """

    force_telegram: bool = False
    silence_window: tuple[dt.time, dt.time] | None = None
    suggestion_min_interval_min: float = 0.0
    # Максимальное количество подсказок в сутки
    daily_limit: int | None = None
    # Ключевые слова, по которым подсказка отменяется
    cancel_keywords: set[str] = dataclass_field(default_factory=set)


class Policy:
    """Инкапсулирует правила выбора канала доставки.

    Объект живёт в рамках процесса и хранит время последней отправки,
    чтобы применять троттлинг между подсказками.
    """

    def __init__(self, config: PolicyConfig) -> None:
        # Конфигурация политики передаётся извне.
        self.config = config
        # Момент времени последней удачной отправки.
        self._last_sent: dt.datetime | None = None
        # Счётчик отправок за текущие сутки
        self._sent_today: int = 0
        self._day: dt.date = dt.date.today()
        # Логгер с отдельным неймспейсом для удобства фильтрации.
        self.log = configure_logging("proactive.policy")
        set_metric("policy.voice_suppressed_night", 0)

    # ------------------------------------------------------------------
    def adapt_from_feedback(self, ratio: dict[str, float] | None = None) -> None:
        """Динамически скорректировать троттлинг и лимиты.

        Параметры ``suggestion_min_interval_min`` и ``daily_limit``
        подстраиваются под реакцию пользователя на подсказки. Когда
        доля принятых подсказок падает ниже 50 %, ассистент становится
        менее навязчивым: увеличивается минимальный интервал и
        снижается дневной лимит. При высокой доле принятий ограничения
        ослабляются.

        :param ratio: словарь долей вида ``{"accepted": x, "rejected": y}``.
            Если не указан, статистика будет получена из
            :func:`analysis.proactivity.feedback_acceptance_ratio`.
        """

        # При необходимости запрашиваем статистику напрямую из слоя анализа
        if ratio is None:
            from analysis.proactivity import feedback_acceptance_ratio

            ratio = feedback_acceptance_ratio()

        accepted = float(ratio.get("accepted", 0.0))
        rejected = float(ratio.get("rejected", 0.0))
        total = accepted + rejected

        if total == 0:
            # Нет данных — ничего не меняем, но оставляем отметку в логе
            self.log.info("adaptation skipped: no feedback")
            return

        accepted_share = accepted / total

        if accepted_share < 0.5:
            # Пользователь чаще отвергает подсказки — уменьшаем активность
            self.config.suggestion_min_interval_min = min(
                self.config.suggestion_min_interval_min + 1, 60
            )
            if self.config.daily_limit is None:
                self.config.daily_limit = 1
            else:
                self.config.daily_limit = max(1, self.config.daily_limit - 1)
            self.log.info(
                "adapted: decrease proactivity",
                extra={
                    "ctx": {
                        "accepted_share": round(accepted_share, 3),
                        "min_interval": self.config.suggestion_min_interval_min,
                        "daily_limit": self.config.daily_limit,
                    }
                },
            )
        else:
            # Подсказки в основном принимаются — можно ускориться
            self.config.suggestion_min_interval_min = max(
                0.0, self.config.suggestion_min_interval_min - 1
            )
            if self.config.daily_limit is None:
                self.config.daily_limit = 1
            else:
                self.config.daily_limit += 1
            self.log.info(
                "adapted: increase proactivity",
                extra={
                    "ctx": {
                        "accepted_share": round(accepted_share, 3),
                        "min_interval": self.config.suggestion_min_interval_min,
                        "daily_limit": self.config.daily_limit,
                    }
                },
            )

    # ------------------------------------------------------------------
    def _in_silence_window(self, moment: dt.time) -> bool:
        """Проверить, попадает ли момент *moment* в тихое окно.

        Когда ``start`` больше ``end``, окно пересекает полночь, поэтому
        проверка выполняется через логическое ``OR``.
        """

        start, end = self.config.silence_window
        if start < end:
            # Обычный случай: окно укладывается в одни календарные сутки.
            return start <= moment <= end
        # Окно проходит через полночь.
        return moment >= start or moment <= end

    # ------------------------------------------------------------------
    def choose_channel(
        self,
        present: bool,
        *,
        now: dt.datetime | None = None,
        text: str | None = None,
    ) -> str | None:
        """Выбрать канал доставки подсказки.

        Последовательно проверяются ограничения: тихие часы,
        ключевые слова отмены, лимит частоты и минимальный интервал.
        После этого определяется канал доставки, учитывая присутствие.
        """

        now = now or dt.datetime.now()

        # --- Тихие часы -------------------------------------------------
        if is_quiet_now():
            self.log.info("suppressed: quiet hours")
            inc_metric("policy.voice_suppressed_night")
            return None

        # --- Отмена по ключевым словам ---------------------------------
        if text and self.config.cancel_keywords:
            lower = text.lower()
            for kw in self.config.cancel_keywords:
                if kw.lower() in lower:
                    self.log.info(
                        "cancelled by keyword", extra={"ctx": {"keyword": kw}}
                    )
                    return None

        # --- Дневной лимит отправок -----------------------------------
        if self.config.daily_limit is not None:
            if now.date() != self._day:
                self._day = now.date()
                self._sent_today = 0
            if self._sent_today >= self.config.daily_limit:
                self.log.info(
                    "daily limit reached",
                    extra={"ctx": {"limit": self.config.daily_limit}},
                )
                return None

        # --- Троттлинг по времени последней отправки -------------------
        if (
            self.config.suggestion_min_interval_min > 0
            and self._last_sent is not None
        ):
            delta = now - self._last_sent
            if delta < dt.timedelta(minutes=self.config.suggestion_min_interval_min):
                # Записываем причину и прекращаем обработку.
                self.log.info(
                    "throttled",
                    extra={"ctx": {"since_last_sec": delta.total_seconds()}},
                )
                return None

        # --- Правила выбора канала -------------------------------------
        channel = "voice"  # базовое предположение
        reasons: list[str] = []
        if self.config.force_telegram:
            # Режим принудительной отправки через Telegram.
            channel = "telegram"
            reasons.append("force_telegram")
        elif not present:
            # Пользователь не рядом — отправляем в Telegram.
            channel = "telegram"
            reasons.append("absent")
        elif self.config.silence_window and self._in_silence_window(now.time()):
            # В «тихое» время голосовые уведомления отключены.
            channel = "telegram"
            reasons.append("silence_window")
            inc_metric("policy.voice_suppressed_night")

        # Запоминаем момент отправки и фиксируем решение в логе.
        self._last_sent = now
        self._sent_today += 1
        self.log.info(
            "channel decided",
            extra={"ctx": {"channel": channel, "reasons": reasons}},
        )
        return channel
