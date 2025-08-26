import random
import time
from enum import Enum

from core.logging_json import configure_logging
from memory import db


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
    """
    Управляет текущим состоянием эмоции и логикой переходов.
    """

    #: Максимально возможный уровень настроения
    _MAX_MOOD = 100
    #: Минимально возможный уровень настроения
    _MIN_MOOD = -100

    def __init__(self):
        # Текущая «видимая» эмоция персонажа
        self.current = Emotion.NEUTRAL
        # Числовой уровень настроения [-100;100]. При запуске
        # восстанавливаем его из постоянного хранилища, чтобы Jarvis
        # помнил предыдущее состояние между перезапусками.
        self.mood = db.get_mood_level()
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
        """Сохранить текущее значение настроения в БД."""
        db.set_mood_level(self.mood)

    def raise_mood(self, delta: int = 10, reason: str = "") -> int:
        """Повысить настроение на ``delta`` и вернуть новое значение.

        Значение всегда ограничивается диапазоном ``[-100; 100]``.
        В лог выводится причина изменения, что помогает анализировать
        реакцию ассистента на взаимодействие с пользователем.
        """
        prev = self.mood
        self.mood = min(self._MAX_MOOD, self.mood + delta)
        self._save_mood()
        self._log.info("mood %s → %s (%s)", prev, self.mood, reason)
        return self.mood

    def drop_mood(self, delta: int = 10, reason: str = "") -> int:
        """Понизить настроение на ``delta`` и вернуть новое значение.

        Значение всегда ограничивается диапазоном ``[-100; 100]``.
        Логирование аналогично ``raise_mood``.
        """
        prev = self.mood
        self.mood = max(self._MIN_MOOD, self.mood - delta)
        self._save_mood()
        self._log.info("mood %s → %s (%s)", prev, self.mood, reason)
        return self.mood

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
