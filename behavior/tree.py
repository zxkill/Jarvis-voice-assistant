from __future__ import annotations
"""Конструктор поведенческого дерева на базе ``py_trees``.

Дерево состоит из трёх ветвей и демонстрирует простую реакцию
ассистента на окружающие события:

1. Если камера видит лицо, ассистент произносит приветствие.
2. Если необходимо моргнуть (например, для анимации), выполняется действие ``Blink``.
3. В остальных случаях запускается ветка ``Idle`` — бездействие.
"""

from py_trees.composites import Selector, Sequence
from py_trees.trees import BehaviourTree

from core.logging_json import configure_logging
from .nodes.conditions import FaceVisible, ShouldBlink
from .nodes.actions import Speak, Blink, Idle

log = configure_logging("behavior.tree")


def create_behavior_tree() -> BehaviourTree:
    """Сформировать и вернуть экземпляр ``BehaviourTree``.

    Возвращаемое дерево не запускает собственный цикл;
    пользователь должен самостоятельно вызывать ``tick()`` у
    полученного экземпляра.
    """

    # Корневой селектор: выбирает первую успешную ветку
    root = Selector(name="root", memory=False)

    # ── Ветка приветствия ────────────────────────────────────────
    greet = Sequence(name="greet", memory=False)
    greet.add_children([FaceVisible(), Speak("Привет! Приятно видеть тебя.")])

    # ── Ветка моргания ────────────────────────────────────────────
    blink = Sequence(name="blink", memory=False)
    blink.add_children([ShouldBlink(), Blink()])

    # ── Ветка ожидания ────────────────────────────────────────────
    idle = Idle()

    root.add_children([greet, blink, idle])
    tree = BehaviourTree(root)
    log.info("поведенческое дерево создано")
    return tree
