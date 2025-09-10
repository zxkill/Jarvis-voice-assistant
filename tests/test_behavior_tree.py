"""Тесты поведенческого дерева на базе ``py_trees``."""

from py_trees.blackboard import Blackboard
from unittest.mock import MagicMock
from types import SimpleNamespace
import sys

from behavior.tree import create_behavior_tree
import behavior.nodes.actions as actions


def test_behavior_tree_transitions(monkeypatch):
    """Дерево должно переключаться между тремя ветками.

    1. Когда лицо видно, выполняется ветка с приветствием.
    2. При флаге ``should_blink`` запускается действие ``Blink``.
    3. Если никаких условий нет, остаётся только ``Idle``.
    """

    # Подменяем внешние зависимости: TTS и драйвер дисплея
    fake_voice = SimpleNamespace(send=MagicMock())
    monkeypatch.setitem(sys.modules, "notifiers.voice", fake_voice)
    driver = MagicMock()
    monkeypatch.setattr(actions, "get_driver", lambda: driver)

    # Очищаем чёрную доску перед запуском теста
    Blackboard.clear()
    tree = create_behavior_tree()

    # ── Ветка приветствия ────────────────────────────────────────
    Blackboard.set("face_visible", True)
    Blackboard.set("should_blink", False)
    tree.tick()
    assert Blackboard.get("spoken") == ["Привет! Приятно видеть тебя."]
    fake_voice.send.assert_called_once_with("Привет! Приятно видеть тебя.")
    # Ветка приветствия не трогает дисплей
    driver.draw.assert_not_called()

    # ── Ветка моргания ────────────────────────────────────────────
    Blackboard.set("face_visible", False)
    Blackboard.set("should_blink", True)
    driver.draw.reset_mock()
    tree.tick()
    assert Blackboard.get("blinked") is True
    driver.draw.assert_called_once()
    item = driver.draw.call_args[0][0]
    assert item.kind == "emotion" and item.payload == "blink"

    # ── Ветка ожидания ────────────────────────────────────────────
    Blackboard.set("should_blink", False)
    driver.draw.reset_mock()
    tree.tick()
    assert Blackboard.get("idled") == 1
    driver.draw.assert_called_once()
    item = driver.draw.call_args[0][0]
    assert item.kind == "emotion" and item.payload == "idle"
