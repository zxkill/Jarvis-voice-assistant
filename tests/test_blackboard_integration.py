"""Проверка интеграции событий и Blackboard."""

from py_trees.common import Status
from py_trees.blackboard import Blackboard

from core.events import Event, publish
from behavior.integration import setup
from behavior.nodes.conditions import FaceVisible


def test_presence_event_updates_face_visible():
    """Событие ``presence.update`` устанавливает флаг видимости лица."""
    # Подписываем обработчики; повторные вызовы безопасны
    setup()
    # На всякий случай сбрасываем предыдущее значение
    Blackboard.set("face_visible", False)

    # Публикуем событие, имитируя появление пользователя
    publish(Event(kind="presence.update", attrs={"present": True}))

    # Узел ``FaceVisible`` должен увидеть установленный флаг и вернуть ``SUCCESS``
    node = FaceVisible()
    node.tick_once()
    assert node.status == Status.SUCCESS
