from __future__ import annotations
"""Действия поведенческого дерева Jarvis.

Каждый узел выполняет простое действие и фиксирует его в ``Blackboard``.
Логи записываются в формате JSON для удобной трассировки.
"""

import py_trees
from py_trees.common import Status
from py_trees.blackboard import Blackboard

from core.logging_json import configure_logging

# Логгер модуля действий
log = configure_logging("behavior.nodes.actions")


class Blink(py_trees.behaviour.Behaviour):
    """Имитация моргания."""

    def __init__(self, name: str = "blink") -> None:
        super().__init__(name)
        self.blackboard = Blackboard()

    def update(self) -> Status:  # noqa: D401
        self.blackboard.set("blinked", True)
        log.info("выполняем моргание", extra={"attrs": {"blinked": True}})
        return Status.SUCCESS


class Speak(py_trees.behaviour.Behaviour):
    """Озвучивание текста."""

    def __init__(self, text: str, name: str = "speak") -> None:
        super().__init__(name)
        self.text = text
        self.blackboard = Blackboard()

    def update(self) -> Status:  # noqa: D401
        try:
            spoken = list(self.blackboard.get("spoken"))
        except KeyError:
            spoken = []
        spoken.append(self.text)
        self.blackboard.set("spoken", spoken)
        log.info("произнесена фраза", extra={"attrs": {"text": self.text}})
        return Status.SUCCESS


class Idle(py_trees.behaviour.Behaviour):
    """Действие по умолчанию, когда нет других задач."""

    def __init__(self, name: str = "idle") -> None:
        super().__init__(name)
        self.blackboard = Blackboard()

    def update(self) -> Status:  # noqa: D401
        try:
            count = self.blackboard.get("idled") + 1
        except KeyError:
            count = 1
        self.blackboard.set("idled", count)
        log.info("ожидание бездействия", extra={"attrs": {"count": count}})
        return Status.SUCCESS
