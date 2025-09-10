"""Модуль управления текущей эмоцией и настроением."""

from __future__ import annotations

# Стандартные библиотеки
import random
import time
from enum import Enum

# Внутренние модули
from core.logging_json import configure_logging
from emotion.mood import Mood


class Emotion(Enum):
    NEUTRAL = "Normal"
    ANGRY = "Angry"
    GLEE = "Glee"
    HAPPY = "Happy"
    SAD = "Sad"
    WORRIED = "Worried"
    THINKING = "Focused"
    ANNOYED = "Annoyed"
    SURPRISED = "Surprised"
    SKEPTIC = "Skeptic"
    FRUSTRATED = "Frustrated"
    UNIMPRESSED = "Unimpressed"
    SLEEPY = "Sleepy"
    SUSPICIOUS = "Suspicious"
    SQUINT = "Squint"
    FURIOUS = "Furious"
    SCARED = "Scared"
    AWE = "Awe"
    TIRED = "Tired"


class EmotionState:
    """Управляет текущей эмоцией и двухмерным настроением."""

    def __init__(self) -> None:
        # Текущая «видимая» эмоция персонажа
        self.current = Emotion.NEUTRAL
        # Объект ``Mood`` восстанавливается из БД и хранит valence/arousal
        self.mood: Mood = Mood.load()
        # Инициализируем логгер для удобной отладки.
        self._log = configure_logging("emotion.state")

    def set(self, emotion: Emotion):
        """Установить новую эмоцию и вернуть её."""
        self.current = emotion
        return self.current

    # ------------------------------------------------------------------
    # Методы работы с уровнем настроения
    # ------------------------------------------------------------------

    def _save_mood(self) -> None:
        """Сохранить текущее состояние ``Mood`` в БД."""

        # Используем встроенный метод ``save``, который записывает valence
        # и arousal в хранилище. Дополнительные поля отсутствуют.
        self.mood.save()

    def raise_mood(self, delta: int = 10, reason: str = "") -> tuple[float, float]:
        """Повысить настроение и вернуть новые координаты.

        Параметр ``delta`` задаётся в условных единицах и преобразуется в
        изменение валентности/возбуждения по шкале [-1.0; 1.0].  Метод
        вызывает :py:meth:`Mood.update`, затем сохраняет результат и
        выводит подробный лог для удобной отладки.
        """

        step = delta / 100.0
        before = self.mood.as_tuple()
        self.mood.update(step, step)
        self._save_mood()
        after = self.mood.as_tuple()
        self._log.info(
            "mood %s → %s (%s)",
            before,
            after,
            reason,
        )
        return after

    def drop_mood(self, delta: int = 10, reason: str = "") -> tuple[float, float]:
        """Понизить настроение и вернуть новые координаты.

        Значение ``delta`` преобразуется в отрицательные дельты и передаётся
        в :py:meth:`Mood.update`.  Это обеспечивает единый API управления
        настроением.
        """

        step = delta / 100.0
        before = self.mood.as_tuple()
        self.mood.update(-step, -step)
        self._save_mood()
        after = self.mood.as_tuple()
        self._log.info(
            "mood %s → %s (%s)",
            before,
            after,
            reason,
        )
        return after

    def get_time_based_emotion(self, hour: int | None = None) -> Emotion:
        """Выбрать базовую эмоцию в зависимости от времени суток.

        Утром показываем сонное выражение лица, днём — радостное,
        а поздно вечером — усталое.  В остальные часы сохраняем
        нейтральное состояние.  Параметр ``hour`` предназначен для
        тестов и позволяет подставить фиксированное время.
        """
        if hour is None:
            hour = time.localtime().tm_hour  # pragma: no cover - время берём из системы

        if 6 <= hour < 12:
            return Emotion.SLEEPY
        if 12 <= hour < 18:
            return Emotion.HAPPY
        if hour >= 22 or hour < 6:
            return Emotion.TIRED
        return Emotion.NEUTRAL

    def get_micro_emotion(self) -> Emotion:
        """Случайная краткосрочная эмоция для оживления простоя."""
        micro_pool = [
            Emotion.SQUINT,
            Emotion.SUSPICIOUS,
            Emotion.GLEE,
            Emotion.AWE,
        ]
        choice = random.choice([e for e in micro_pool if e != self.current])
        self.current = choice
        return choice

    def get_next_idle(self) -> Emotion:
        """Следующая эмоция для режима простоя.

        Сначала выбираем базовую эмоцию по времени суток.  Если она
        отличается от текущей, переключаемся на неё.  В противном
        случае возвращаем случайную «микро‑эмоцию», чтобы персонаж не
        казался застывшим.
        """
        base = self.get_time_based_emotion()
        if base != self.current:
            self.current = base
            return base
        return self.get_micro_emotion()

    def get_thinking(self) -> Emotion:
        """Возвращает состояние мысли (используется при обработке запроса)."""
        self.current = Emotion.THINKING
        return self.current
