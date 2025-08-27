"""Тесты поведенческого дерева на базе ``py_trees``."""

from py_trees.blackboard import Blackboard

from behavior.tree import create_behavior_tree


def test_behavior_tree_transitions():
    """Дерево должно переключаться между тремя ветками.

    1. Когда лицо видно, выполняется ветка с приветствием.
    2. При флаге ``should_blink`` запускается действие ``Blink``.
    3. Если никаких условий нет, остаётся только ``Idle``.
    """

    tree = create_behavior_tree()

    # ── Ветка приветствия ────────────────────────────────────────
    Blackboard.set("face_visible", True)
    Blackboard.set("should_blink", False)
    tree.tick()
    assert Blackboard.get("spoken") == ["Привет! Приятно видеть тебя."]
    assert not Blackboard.exists("blinked")

    # ── Ветка моргания ────────────────────────────────────────────
    Blackboard.set("face_visible", False)
    Blackboard.set("should_blink", True)
    tree.tick()
    assert Blackboard.get("blinked") is True

    # ── Ветка ожидания ────────────────────────────────────────────
    Blackboard.set("should_blink", False)
    tree.tick()
    assert Blackboard.get("idled") == 1
