"""Абстракции сенсоров настроения и их примерные реализации.

Модуль предоставляет базовый класс :class:`MoodSensor`, который
упрощает интеграцию различных источников данных с системой эмоций.
Каждый сенсор отвечает за вычисление дельт по осям ``valence`` и
``arousal`` и применяет их к объекту :class:`emotion.mood.Mood`.

Сенсоры можно вызывать периодически из планировщика или любого
другого места приложения.  Для примера показан сенсор времени суток,
который повышает настроение утром и понижает вечером.
"""

from __future__ import annotations

# Стандартные библиотеки
import abc
import datetime as _dt
import logging
from typing import Callable, Tuple

from emotion.mood import Mood

# Логгер верхнего уровня для всех сенсоров настроения
log = logging.getLogger("emotion.sensors")


class MoodSensor(abc.ABC):
    """Абстрактный базовый класс для сенсоров настроения.

    Наследники должны переопределить метод :meth:`measure`, который
    возвращает кортеж ``(valence_delta, arousal_delta, reason)``.
    Метод :meth:`read_and_update` обрабатывает результат измерения и
    при необходимости обновляет переданный объект настроения.
    """

    def __init__(self, mood: Mood, name: str) -> None:
        self._mood = mood
        self._name = name
        self._logger = logging.getLogger(f"emotion.sensors.{name}")

    # ------------------------------------------------------------------ utils
    @abc.abstractmethod
    def measure(self) -> Tuple[float, float, str]:
        """Выполнить измерение и вернуть дельты настроения.

        Возвращается кортеж ``(valence_delta, arousal_delta, reason)``.
        Дельты могут быть нулевыми, тогда настроение не будет изменено.
        """

    # ----------------------------------------------------------------- main API
    def read_and_update(self) -> None:
        """Считать данные с сенсора и при необходимости обновить настроение."""

        valence_delta, arousal_delta, reason = self.measure()
        ctx = {
            "sensor": self._name,
            "valence_delta": valence_delta,
            "arousal_delta": arousal_delta,
            "reason": reason,
        }
        if valence_delta or arousal_delta:
            # Изменение настроения фиксируем в информационном логе
            self._logger.info("mood adjusted", extra={"ctx": ctx})
            self._mood.update(valence_delta, arousal_delta)
            # Сохранение в БД делаем отдельно для упрощения тестирования
            self._mood.save()
        else:
            # Нулевые дельты полезны для диагностики
            self._logger.debug("no mood change", extra={"ctx": ctx})


class TimeOfDayMoodSensor(MoodSensor):
    """Простейший сенсор, корректирующий настроение по времени суток.

    Утром значения ``valence`` и ``arousal`` повышаются, днём остаются
    неизменными, а вечером и ночью снижаются.  Такой сенсор может быть
    полезен для имитации естественных суточных колебаний настроения.
    """

    def __init__(
        self,
        mood: Mood,
        clock: Callable[[], _dt.datetime] | None = None,
    ) -> None:
        super().__init__(mood, "time_of_day")
        # ``clock`` позволяет внедрять контроль времени в тестах
        self._clock = clock or _dt.datetime.now

    def measure(self) -> Tuple[float, float, str]:
        """Рассчитать влияние текущего времени суток на настроение."""

        hour = self._clock().hour
        if 6 <= hour < 12:
            # Утро – лёгкий подъём
            return 0.2, 0.3, "morning"
        if 12 <= hour < 18:
            # Днём настроение не меняем
            return 0.0, 0.0, "afternoon"
        # Вечером настроение слегка падает
        return -0.2, -0.3, "night"
