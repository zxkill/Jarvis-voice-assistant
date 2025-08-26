import types
import emotion.sounds as sounds
from core import events as core_events


def test_idle_breath_timer_reschedule(monkeypatch):
    """Таймер должен перезапускаться при смене присутствия пользователя."""

    delays: list[float] = []
    timers: list[object] = []

    class DummyTimer:
        """Заглушка для ``threading.Timer`` без фактического ожидания."""

        def __init__(self, delay, callback):
            delays.append(delay)
            self.cancelled = False
            timers.append(self)

        def start(self) -> None:
            pass  # запуск таймера не нужен

        def cancel(self) -> None:
            self.cancelled = True

    # Подменяем ``threading.Timer`` на заглушку, чтобы проверять параметры
    # планирования без реального ожидания 15 минут.
    monkeypatch.setattr(
        sounds,
        "threading",
        types.SimpleNamespace(Timer=lambda d, cb: DummyTimer(d, cb)),
    )

    driver = sounds.EmotionSoundDriver()
    # После инициализации должен быть запланирован первый запуск "дыхания"
    assert len(delays) == 1
    assert delays[0] >= sounds.MIN_IDLE_BREATH_COOLDOWN

    # При появлении человека таймер отменяется и заново не создаётся
    driver._on_presence_update(core_events.Event("presence.update", {"present": True}))
    assert timers[0].cancelled
    assert len(delays) == 1

    # После ухода человека планируется новый таймер
    driver._on_presence_update(core_events.Event("presence.update", {"present": False}))
    assert len(delays) == 2
    assert delays[1] >= sounds.MIN_IDLE_BREATH_COOLDOWN
