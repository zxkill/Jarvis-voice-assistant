"""Тесты для EmotionDisplayDriver.

Проверяем корректную отрисовку эмоции ``TIRED`` на дисплее, когда
аппаратная часть M5Stack не поддерживает соответствующую иконку.
"""

import core.events as core_events
from emotion.state import Emotion


class _DummyDisplayDriver:
    """Простейший драйвер для перехвата вызовов ``draw``.

    Каждое нарисованное сообщение сохраняем в списке ``calls`` для
    последующей проверки в тестах.
    """

    def __init__(self):
        self.calls: list = []

    def draw(self, item):  # pragma: no cover - крайне простой метод
        self.calls.append(item)

    def process_events(self):  # pragma: no cover - в тесте не используется
        return


def test_tired_fallback(monkeypatch):
    """Эмоция ``TIRED`` должна отображаться как ``Sleepy`` + текст."""

    # очищаем глобальных подписчиков между тестами
    core_events._subscribers.clear()
    core_events._global_subscribers.clear()

    dummy = _DummyDisplayDriver()
    # подменяем драйвер дисплея нашим заглушечным
    monkeypatch.setattr("emotion.drivers.get_driver", lambda: dummy)

    # импорт после подмены, чтобы EmotionDisplayDriver взял нужный драйвер
    from emotion.drivers import EmotionDisplayDriver

    EmotionDisplayDriver()  # подписка на событие

    # публикуем событие усталости
    core_events.publish(core_events.Event(kind="emotion_changed", attrs={"emotion": Emotion.TIRED}))

    # должны получить две отрисовки: sleepy + текст
    assert dummy.calls[0].kind == "emotion" and dummy.calls[0].payload == Emotion.SLEEPY.value
    assert dummy.calls[1].kind == "text" and "zZ" in dummy.calls[1].payload

    # смена эмоции должна очистить текст и показать новую иконку
    core_events.publish(core_events.Event(kind="emotion_changed", attrs={"emotion": Emotion.HAPPY}))
    assert dummy.calls[2].kind == "text" and dummy.calls[2].payload is None
    assert dummy.calls[3].kind == "emotion" and dummy.calls[3].payload == Emotion.HAPPY.value

