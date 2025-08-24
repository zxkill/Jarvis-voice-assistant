import time

from emotion.state import EmotionState, Emotion
from core.logging_json import configure_logging
from core import events as core_events

log = configure_logging("emotion.manager")


class EmotionManager:
    """Управляет сменой эмоций через глобальный event bus."""

    def __init__(self) -> None:
        self._state = EmotionState()
        self._prev_emotion = self._state.current

        # Подписываемся на события
        core_events.subscribe("user_query_started", self._on_query_started)
        core_events.subscribe("user_query_ended", self._on_query_ended)
        core_events.subscribe("emotion_changed", self._on_external_change)

    def start(self) -> None:
        """Опубликовать начальное состояние."""
        self._publish_emotion(self._state.current)

    def stop(self) -> None:  # pragma: no cover - для совместимости API
        """Совместимость с прежним API, активных потоков нет."""
        pass

    def _on_external_change(self, event: core_events.Event) -> None:
        """Обновить локальное состояние при смене эмоции и вывести её в лог."""
        new = event.attrs["emotion"]
        prev = self._state.current
        log.info("emotion %s → %s", prev.value, new.value)
        self._state.set(new)

    def _on_query_started(self, event: core_events.Event) -> None:
        """При начале обработки пользовательского запроса — эмоция THINKING."""
        self._prev_emotion = self._state.current
        emo = self._state.get_thinking()
        log.debug("user_query_started → %s", emo.value)
        self._publish_emotion(emo)

    def _on_query_ended(self, event: core_events.Event) -> None:
        """При завершении обработки запроса — вернуться к предыдущей эмоции."""
        log.debug("user_query_ended → wait 1s")
        time.sleep(1)
        emo = self._state.set(self._prev_emotion)
        log.debug("user_query_ended → %s", emo.value)
        self._publish_emotion(emo)

    def _publish_emotion(self, emotion: Emotion) -> None:
        """Публикует событие смены эмоции."""
        log.debug("Publishing emotion_changed(%s)", emotion.value)
        core_events.publish(
            core_events.Event(kind="emotion_changed", attrs={"emotion": emotion})
        )

