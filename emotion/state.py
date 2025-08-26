import random
import time
from enum import Enum


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

    def __init__(self):
        self.current = Emotion.NEUTRAL

    def set(self, emotion: Emotion):
        """Установить новую эмоцию и вернуть её."""
        self.current = emotion
        return self.current

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
