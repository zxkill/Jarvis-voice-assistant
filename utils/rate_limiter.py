"""Простейший лимитер частоты вызовов.

Класс помогает ограничивать число операций в единицу времени, что
полезно при обращении к внешним API или выполнении ресурсоёмких задач.

Пример
======

>>> t = iter([0, 0.5, 1.1])
>>> rl = RateLimiter(1, 1, time_func=lambda: next(t))
>>> rl.allow()
True
>>> rl.allow()
False
>>> rl.allow()
True
"""

from __future__ import annotations

import logging
import time
from collections import deque
from typing import Callable, Deque

# Логгер позволяет отслеживать решение лимитера в журналах.
log = logging.getLogger(__name__)


class RateLimiter:
    """Ограничитель количества операций за интервал времени.

    :param max_calls: максимальное число вызовов за период
    :param period: длина периода в секундах
    :param time_func: функция получения текущего времени. По умолчанию
        используется ``time.monotonic``.  Параметр упростит тестирование,
        позволяя подменять источник времени.
    """

    def __init__(
        self,
        max_calls: int,
        period: float,
        time_func: Callable[[], float] = time.monotonic,
    ) -> None:
        self.max_calls = max_calls
        self.period = period
        self.time_func = time_func
        self._calls: Deque[float] = deque()
        log.debug(
            "Создан RateLimiter max_calls=%s period=%s", max_calls, period
        )

    def allow(self) -> bool:
        """Проверить, можно ли выполнить очередной вызов.

        :return: ``True``, если лимит не превышен, иначе ``False``.
        """
        now = self.time_func()
        # Удаляем из очереди вызовы, вышедшие за пределы периода.
        while self._calls and self._calls[0] <= now - self.period:
            expired = self._calls.popleft()
            log.debug("Удалён истёкший вызов %s", expired)

        if len(self._calls) < self.max_calls:
            self._calls.append(now)
            log.debug(
                "Разрешён вызов, текущее количество=%s", len(self._calls)
            )
            return True

        log.debug(
            "Отклонён вызов, текущее количество=%s", len(self._calls)
        )
        return False
