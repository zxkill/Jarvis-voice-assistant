from __future__ import annotations
"""Действия поведенческого дерева Jarvis.

Каждый узел выполняет простое действие и фиксирует его в ``Blackboard``.
Логи записываются в формате JSON для удобной трассировки.
"""

import py_trees
from py_trees.common import Status
from py_trees.blackboard import Blackboard

from utils.rng import lognormal
from utils.noise import perlin
import random

from core.logging_json import configure_logging
from display import get_driver, DisplayItem

# Логгер модуля действий
log = configure_logging("behavior.nodes.actions")


class Blink(py_trees.behaviour.Behaviour):
    """Имитация моргания."""

    def __init__(self, name: str = "blink") -> None:
        super().__init__(name)
        self.blackboard = Blackboard()

    def update(self) -> Status:  # noqa: D401
        # Фиксируем факт моргания на чёрной доске до обращения к внешним сервисам
        self.blackboard.set("blinked", True)
        log.info("выполняем моргание", extra={"attrs": {"blinked": True}})
        try:
            # Публикуем событие моргания на дисплей
            driver = get_driver()
            driver.draw(DisplayItem(kind="emotion", payload="blink"))
        except Exception:  # pragma: no cover - непредвиденные ошибки отображения
            log.exception("ошибка отображения моргания")
            return Status.FAILURE
        return Status.SUCCESS


class Speak(py_trees.behaviour.Behaviour):
    """Озвучивание текста."""

    def __init__(self, text: str, name: str = "speak") -> None:
        super().__init__(name)
        self.text = text
        self.blackboard = Blackboard()

    def update(self) -> Status:  # noqa: D401
        # Сохраняем все сказанные фразы в истории Blackboard
        try:
            spoken = list(self.blackboard.get("spoken"))
        except KeyError:
            spoken = []
        spoken.append(self.text)
        self.blackboard.set("spoken", spoken)
        log.info("произнесена фраза", extra={"attrs": {"text": self.text}})
        try:
            # Импортируем модуль голосовых уведомлений лениво,
            # чтобы избежать тяжёлых зависимостей при старте.
            from notifiers import voice

            voice.send(self.text)
        except Exception:  # pragma: no cover - внешние ошибки TTS
            log.exception("ошибка отправки текста в TTS")
            return Status.FAILURE
        return Status.SUCCESS


class Idle(py_trees.behaviour.Behaviour):
    """Действие по умолчанию, когда нет других задач."""

    def __init__(self, name: str = "idle") -> None:
        super().__init__(name)
        self.blackboard = Blackboard()

    def update(self) -> Status:  # noqa: D401
        # Увеличиваем счётчик периодов ожидания в Blackboard
        try:
            count = self.blackboard.get("idled") + 1
        except KeyError:
            count = 1
        self.blackboard.set("idled", count)
        log.info("ожидание бездействия", extra={"attrs": {"count": count}})
        try:
            # Отправляем на дисплей маркер режима ожидания
            driver = get_driver()
            driver.draw(DisplayItem(kind="emotion", payload="idle"))
        except Exception:  # pragma: no cover - непредвиденные ошибки отображения
            log.exception("ошибка отображения режима ожидания")
            return Status.FAILURE
        return Status.SUCCESS


def blink(start: float = 0.0, mu: float = 0.3, sigma: float = 0.1, seed: int | None = None):
    """Генератор времени морганий.

    Для интервалов между морганиями используется логнормальное
    распределение. Возвращает абсолютные метки времени (в секундах)
    от ``start``. Каждое запланированное моргание логируется вместе
    с рассчитанным интервалом и параметрами распределения.
    """

    rng = random.Random(seed)
    t = start
    while True:
        interval = lognormal(mu, sigma, rng=rng)
        t += interval
        log.info(
            "запланировано моргание",
            extra={"attrs": {"next_time": t, "interval": interval, "mu": mu, "sigma": sigma}},
        )
        yield t


def micro_saccade(time_point: float, amplitude: float = 1.0, seed: int = 0) -> float:
    """Вычислить угол смещения взгляда для микро-саккады.

    Плавный дрейф рассчитывается через шум Перлина. Результат
    ограничивается значением ``amplitude`` и логируется для
    упрощения отладки.
    """

    drift = perlin(time_point, seed=seed)
    angle = max(min(drift * amplitude, amplitude), -amplitude)
    log.info(
        "микро-саккада",
        extra={
            "attrs": {
                "time": time_point,
                "drift": drift,
                "angle": angle,
                "amplitude": amplitude,
            }
        },
    )
    return angle
