from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from core.logging_json import configure_logging
from core.metrics import inc_metric, set_metric


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
        # Логгер с отдельным неймспейсом для удобства фильтрации.
        self.log = configure_logging("proactive.policy")
        set_metric("policy.voice_suppressed_night", 0)

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
    def choose_channel(self, present: bool, now: dt.datetime | None = None) -> str | None:
        """Выбрать канал доставки подсказки.

        Порядок работы:
        1. Проверяется интервал с последней отправки. Если он меньше
           ``suggestion_min_interval_min`` — подсказка игнорируется.
        2. По умолчанию выбирается голосовой канал. Он заменяется на
           Telegram, если выполнено одно из правил: принудительный режим,
           пользователь отсутствует или наступило «тихое» окно.
        3. Решение логируется вместе с причинами.

        Parameters
        ----------
        present:
            Присутствует ли пользователь физически рядом с устройством.
        now:
            Текущее время; используется в тестах, в продакшене
            берётся ``datetime.now``.

        Returns
        -------
        str | None
            Имя канала (``"voice"`` или ``"telegram"``) либо ``None``,
            если троттлинг заблокировал отправку.
        """

        now = now or dt.datetime.now()

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
        self.log.info(
            "channel decided",
            extra={"ctx": {"channel": channel, "reasons": reasons}},
        )
        return channel
