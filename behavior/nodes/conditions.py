from __future__ import annotations
"""Условия для поведенческого дерева Jarvis.

Каждый узел читает соответствующее значение из ``Blackboard`` и
возвращает ``py_trees.common.Status.SUCCESS`` только если условие
выполнено. Во всех узлах реализовано подробное JSON‑логирование,
что облегчает отладку и анализ поведения ассистента.
"""

import py_trees
from py_trees.common import Status
from py_trees.blackboard import Blackboard

from core.logging_json import configure_logging

# Инициализируем логгер для модуля условий
log = configure_logging("behavior.nodes.conditions")


class FaceVisible(py_trees.behaviour.Behaviour):
    """Проверка видимости лица пользователя.

    Узел читает флаг ``face_visible`` из ``Blackboard`` и сообщает
    об этом в логах. Если лицо обнаружено камерой, возвращается
    ``SUCCESS``, иначе ``FAILURE``.
    """

    def __init__(self, name: str = "face_visible") -> None:
        super().__init__(name)
        # Получаем глобальный ``Blackboard``
        self.blackboard = Blackboard()

    def update(self) -> Status:  # noqa: D401 - поведение описано в docstring
        try:
            visible = self.blackboard.get("face_visible")
        except KeyError:
            visible = False
        log.info(
            "проверка наличия лица",
            extra={"attrs": {"face_visible": visible}},
        )
        return Status.SUCCESS if visible else Status.FAILURE


class ShouldBlink(py_trees.behaviour.Behaviour):
    """Нужно ли моргнуть.

    Вспомогательное условие: проверяет флаг ``should_blink``.
    Это позволяет протестировать переход на ветку с действием
    ``Blink`` даже при отсутствии лица в кадре.
    """

    def __init__(self, name: str = "should_blink") -> None:
        super().__init__(name)
        self.blackboard = Blackboard()

    def update(self) -> Status:  # noqa: D401
        try:
            need = self.blackboard.get("should_blink")
        except KeyError:
            need = False
        log.info(
            "проверка необходимости моргания",
            extra={"attrs": {"should_blink": need}},
        )
        return Status.SUCCESS if need else Status.FAILURE
