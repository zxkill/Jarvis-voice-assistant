"""Юнит-тесты отдельных действий поведенческого дерева."""

from unittest.mock import MagicMock
from py_trees.common import Status
from types import SimpleNamespace
import sys

import behavior.nodes.actions as actions
from behavior.nodes.actions import Speak, Blink, Idle


def test_speak_handles_tts_error(monkeypatch):
    """При ошибке TTS узел возвращает FAILURE, но история сохраняется."""
    node = Speak("test")

    def broken_send(_: str) -> None:
        raise RuntimeError("boom")

    fake_voice = SimpleNamespace(send=broken_send)
    monkeypatch.setitem(sys.modules, "notifiers.voice", fake_voice)
    status = node.update()
    assert status == Status.FAILURE
    assert node.blackboard.get("spoken") == ["test"]


def test_blink_handles_display_error(monkeypatch):
    """Ошибки драйвера дисплея приводят к FAILURE, но пометка о моргании сохраняется."""
    node = Blink()
    driver = MagicMock()
    driver.draw.side_effect = RuntimeError("boom")
    monkeypatch.setattr(actions, "get_driver", lambda: driver)
    status = node.update()
    assert status == Status.FAILURE
    assert node.blackboard.get("blinked") is True


def test_idle_handles_display_error(monkeypatch):
    """Режим ожидания корректно логирует сбой дисплея."""
    node = Idle()
    driver = MagicMock()
    driver.draw.side_effect = RuntimeError("boom")
    monkeypatch.setattr(actions, "get_driver", lambda: driver)
    status = node.update()
    assert status == Status.FAILURE
    assert node.blackboard.get("idled") == 1
